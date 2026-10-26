"""Operator console for whoever runs this deployment (ADMIN_EMAILS): leads, workspaces, plans, Stripe."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import LEAD_STATUSES, Membership, Organization, QueryLog, SalesLead, SemanticModel, User
from ..plans import PLANS
from .. import security
from ..platform_settings import StripeSetupError, add_operator, connect_stripe, disconnect_stripe, stripe_config
from ..services import month_start, plan_for
from .deps import (
    flash,
    is_operator_candidate,
    is_platform_admin,
    redirect,
    render,
    require_platform_admin,
    require_user,
    verify_csrf,
)

router = APIRouter(prefix="/admin")
claim_throttle = security.LoginThrottle(max_failures=10, window_seconds=3600)


@router.get("/claim")
def admin_claim_form(request: Request, user: User = Depends(require_user)):
    if not is_operator_candidate(user):
        raise HTTPException(404)
    if is_platform_admin(user):
        return redirect("/admin")
    return render(request, "admin/claim.html", None, user=user)


@router.post("/claim", dependencies=[Depends(verify_csrf)])
def admin_claim(request: Request, code: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not is_operator_candidate(user):
        raise HTTPException(404)
    key = f"claim|{user.id}"
    if claim_throttle.blocked(key) or not security.operator_code_matches(code):
        claim_throttle.failed(key)
        return render(request, "admin/claim.html", None, user=user, error="That code is not valid.", status_code=400)
    add_operator(db, user.id)
    flash(request, "Operator access granted.", "success")
    return redirect("/admin")


def mrr_cents(db: Session) -> int:
    total = 0
    for org in db.scalars(select(Organization).where(Organization.plan.in_(("pro", "business")))):
        plan = plan_for(org)
        if plan.key == org.plan:
            total += plan.monthly_price_cents
    return total


@router.get("")
def admin_home(request: Request, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    since = datetime.now(timezone.utc) - timedelta(days=30)
    stats = {
        "users": db.scalar(select(func.count(User.id))) or 0,
        "workspaces": db.scalar(select(func.count(Organization.id))) or 0,
        "paid": db.scalar(select(func.count(Organization.id)).where(Organization.plan != "free")) or 0,
        "mrr": mrr_cents(db) / 100,
        "queries_30d": db.scalar(
            select(func.count(QueryLog.id)).where(QueryLog.created_at >= since, QueryLog.status == "ok")
        )
        or 0,
        "new_leads": db.scalar(select(func.count(SalesLead.id)).where(SalesLead.status == "new")) or 0,
    }
    leads = list(db.scalars(select(SalesLead).order_by(SalesLead.id.desc()).limit(5)))
    return render(
        request,
        "admin/index.html",
        None,
        user=user,
        stats=stats,
        leads=leads,
        stripe=stripe_config(db),
    )


@router.post("/stripe/connect", dependencies=[Depends(verify_csrf)])
def admin_stripe_connect(
    request: Request,
    secret_key: str = Form(...),
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    if stripe_config(db).source == "env":
        flash(request, "Stripe is configured through environment variables; change it there.", "error")
        return redirect("/admin")
    try:
        config = connect_stripe(db, secret_key)
    except StripeSetupError as e:
        db.rollback()
        flash(request, str(e), "error")
        return redirect("/admin")
    mode = "LIVE" if config.live_mode else "test"
    flash(
        request,
        f"Stripe connected in {mode} mode: Pro and Business prices, the webhook and the customer portal are set up. "
        "Customers can now upgrade from their Billing page.",
        "success",
    )
    return redirect("/admin")


@router.post("/stripe/disconnect", dependencies=[Depends(verify_csrf)])
def admin_stripe_disconnect(request: Request, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    disconnect_stripe(db)
    flash(request, "Stripe disconnected. Existing subscriptions keep running in Stripe.", "success")
    return redirect("/admin")


@router.get("/workspaces")
def admin_workspaces(request: Request, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    members = dict(
        db.execute(select(Membership.organization_id, func.count(Membership.id)).group_by(Membership.organization_id)).all()
    )
    models = dict(
        db.execute(
            select(SemanticModel.organization_id, func.count(SemanticModel.id)).group_by(SemanticModel.organization_id)
        ).all()
    )
    queries = dict(
        db.execute(
            select(QueryLog.organization_id, func.count(QueryLog.id))
            .where(QueryLog.created_at >= month_start(), QueryLog.status == "ok")
            .group_by(QueryLog.organization_id)
        ).all()
    )
    owners = {
        m.organization_id: m.user.email
        for m in db.scalars(select(Membership).where(Membership.role == "owner"))
    }
    orgs = list(db.scalars(select(Organization).order_by(Organization.id.desc())))
    return render(
        request,
        "admin/workspaces.html",
        None,
        user=user,
        orgs=orgs,
        members=members,
        models=models,
        queries=queries,
        owners=owners,
        plans=list(PLANS.values()),
    )


@router.post("/workspaces/{org_id}/plan", dependencies=[Depends(verify_csrf)])
def admin_set_plan(
    request: Request,
    org_id: int,
    plan: str = Form(...),
    next: str = Form("/admin/workspaces"),
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    org = db.get(Organization, org_id)
    if org is None or plan not in PLANS:
        raise HTTPException(404)
    org.plan = plan
    if not org.stripe_subscription_id:
        # Invoiced/manual plans are active until changed here.
        org.subscription_status = None if plan in ("free", "enterprise") else "active"
    db.commit()
    flash(request, f"{org.name} is now on {PLANS[plan].name}.", "success")
    return redirect(next if next.startswith("/admin") else "/admin/workspaces")


@router.get("/leads")
def admin_leads(request: Request, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    leads = list(db.scalars(select(SalesLead).order_by(SalesLead.id.desc()).limit(500)))
    return render(request, "admin/leads.html", None, user=user, leads=leads, statuses=LEAD_STATUSES, plans=list(PLANS.values()))


@router.post("/leads/{lead_id}/status", dependencies=[Depends(verify_csrf)])
def admin_lead_status(
    request: Request,
    lead_id: int,
    status: str = Form(...),
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    lead = db.get(SalesLead, lead_id)
    if lead is None or status not in LEAD_STATUSES:
        raise HTTPException(404)
    lead.status = status
    db.commit()
    return redirect("/admin/leads")
