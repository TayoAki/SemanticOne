"""Stripe subscriptions: Checkout to upgrade, Customer Portal to manage, webhooks to sync plan state."""

from __future__ import annotations

import logging

import stripe
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import Organization
from .plans import PLANS
from .platform_settings import StripeConfig, stripe_config
from .services import usage_summary
from .web.deps import Ctx, flash, redirect, render, require_ctx, verify_csrf

log = logging.getLogger("semanticone.billing")
router = APIRouter()


@router.get("/app/billing")
def billing_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    config = stripe_config(ctx.db)
    return render(
        request,
        "app/billing.html",
        ctx,
        usage=usage_summary(ctx.db, ctx.org),
        plans=list(PLANS.values()),
        billing_enabled=config.enabled,
        price_available={k: bool(config.price_for(k)) for k in PLANS},
    )


@router.post("/app/billing/checkout", dependencies=[Depends(verify_csrf)])
def checkout(request: Request, plan: str = Form(...), ctx: Ctx = Depends(require_ctx)):
    ctx.require_admin()
    settings = get_settings()
    config = stripe_config(ctx.db)
    price = config.price_for(plan)
    if not config.enabled or not price:
        flash(request, "Online checkout is not configured. Contact sales to upgrade.", "error")
        return redirect(f"/contact?plan={plan}")
    client = config.client()
    org = ctx.org
    if not org.stripe_customer_id:
        customer = client.v1.customers.create(
            params={"email": ctx.user.email, "name": org.name, "metadata": {"org_id": str(org.id), "org_slug": org.slug}}
        )
        org.stripe_customer_id = customer.id
        ctx.db.commit()
    if org.stripe_subscription_id and org.subscription_status in ("active", "trialing", "past_due"):
        # Plan changes for existing subscribers go through the portal (handles proration).
        return _portal_redirect(config, org)
    session = client.v1.checkout.sessions.create(
        params={
            "mode": "subscription",
            "customer": org.stripe_customer_id,
            "line_items": [{"price": price, "quantity": 1}],
            "client_reference_id": str(org.id),
            "metadata": {"org_id": str(org.id), "plan": plan},
            "subscription_data": {"metadata": {"org_id": str(org.id), "plan": plan}},
            "allow_promotion_codes": True,
            "success_url": f"{settings.base_url}/app/billing?checkout=success",
            "cancel_url": f"{settings.base_url}/app/billing?checkout=cancelled",
        }
    )
    return redirect(session.url)


def _portal_redirect(config: StripeConfig, org: Organization):
    params = {"customer": org.stripe_customer_id, "return_url": f"{get_settings().base_url}/app/billing"}
    if config.portal_config:
        params["configuration"] = config.portal_config
    session = config.client().v1.billing_portal.sessions.create(params=params)
    return redirect(session.url)


@router.post("/app/billing/portal", dependencies=[Depends(verify_csrf)])
def portal(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.require_admin()
    config = stripe_config(ctx.db)
    if not config.enabled or not ctx.org.stripe_customer_id:
        flash(request, "There is no billing account for this workspace yet.", "error")
        return redirect("/app/billing")
    return _portal_redirect(config, ctx.org)


# --- webhooks ----------------------------------------------------------------


def _org_for(db: Session, obj: dict) -> Organization | None:
    metadata = obj.get("metadata") or {}
    org_id = metadata.get("org_id") or obj.get("client_reference_id")
    if org_id:
        org = db.get(Organization, int(org_id))
        if org is not None:
            return org
    customer = obj.get("customer")
    if customer:
        return db.scalar(select(Organization).where(Organization.stripe_customer_id == customer))
    return None


def apply_subscription(config: StripeConfig, org: Organization, subscription: dict) -> None:
    status = subscription.get("status")
    items = (subscription.get("items") or {}).get("data") or []
    price_id = ((items[0].get("price") or {}).get("id")) if items else None
    plan = config.plan_for_price(price_id) or (subscription.get("metadata") or {}).get("plan")
    org.stripe_subscription_id = subscription.get("id")
    org.subscription_status = status
    if subscription.get("customer"):
        org.stripe_customer_id = subscription["customer"]
    if status in ("active", "trialing", "past_due") and plan in PLANS:
        org.plan = plan
    elif status in ("canceled", "unpaid", "incomplete_expired"):
        org.plan = "free"


def handle_event(db: Session, event: dict) -> str:
    config = stripe_config(db)
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed":
        org = _org_for(db, obj)
        if org is None:
            return "ignored: no org"
        if obj.get("customer"):
            org.stripe_customer_id = obj["customer"]
        subscription_id = obj.get("subscription")
        if subscription_id and config.secret_key:
            subscription = config.client().v1.subscriptions.retrieve(subscription_id)
            apply_subscription(config, org, subscription.to_dict())
        else:
            plan = (obj.get("metadata") or {}).get("plan")
            if plan in PLANS:
                org.plan = plan
                org.subscription_status = "active"
                org.stripe_subscription_id = subscription_id
        db.commit()
        return f"org {org.id} -> {org.plan}"
    if kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        org = _org_for(db, obj)
        if org is None:
            return "ignored: no org"
        if kind == "customer.subscription.deleted":
            obj = {**obj, "status": "canceled"}
        apply_subscription(config, org, obj)
        db.commit()
        return f"org {org.id} -> {org.plan} ({org.subscription_status})"
    if kind == "invoice.payment_failed":
        org = _org_for(db, obj)
        if org is not None:
            org.subscription_status = "past_due"
            db.commit()
            return f"org {org.id} past_due"
    return "ignored"


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    secret = stripe_config(db).webhook_secret
    if not secret:
        raise HTTPException(status_code=404)
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid signature") from None
    outcome = handle_event(db, event.to_dict() if hasattr(event, "to_dict") else dict(event))
    log.info("stripe %s: %s", event.get("type") if hasattr(event, "get") else "?", outcome)
    return JSONResponse({"received": True, "outcome": outcome})
