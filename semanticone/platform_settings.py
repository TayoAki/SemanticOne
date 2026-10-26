"""Operator-level configuration stored in the database (set from the admin console).

Environment variables always win; database values let the operator turn on Stripe
without redeploying or handling secrets outside the app.
"""

from __future__ import annotations

from dataclasses import dataclass

import stripe
from sqlalchemy.orm import Session

from . import security
from .config import get_settings
from .models import PlatformSetting
from .plans import PLANS

STRIPE_KEYS = ("stripe_secret_key", "stripe_webhook_secret", "stripe_price_pro", "stripe_price_business", "stripe_portal_config")
_SECRET_KEYS = {"stripe_secret_key", "stripe_webhook_secret"}

WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
]


def get_setting(db: Session, key: str) -> str:
    row = db.get(PlatformSetting, key)
    if row is None or not row.value:
        return ""
    return security.decrypt(row.value) if row.encrypted else row.value


def set_setting(db: Session, key: str, value: str) -> None:
    encrypted = key in _SECRET_KEYS
    stored = security.encrypt(value) if (encrypted and value) else value
    row = db.get(PlatformSetting, key)
    if row is None:
        db.add(PlatformSetting(key=key, value=stored, encrypted=encrypted))
    else:
        row.value, row.encrypted = stored, encrypted


def operator_user_ids(db: Session) -> set[int]:
    raw = get_setting(db, "operator_user_ids")
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def add_operator(db: Session, user_id: int) -> None:
    ids = operator_user_ids(db) | {user_id}
    set_setting(db, "operator_user_ids", ",".join(str(i) for i in sorted(ids)))
    db.commit()


@dataclass(frozen=True)
class StripeConfig:
    secret_key: str
    webhook_secret: str
    price_pro: str
    price_business: str
    portal_config: str
    source: str  # "env", "admin console" or "none"

    @property
    def enabled(self) -> bool:
        return bool(self.secret_key and (self.price_pro or self.price_business))

    @property
    def live_mode(self) -> bool:
        return self.secret_key.startswith(("sk_live_", "rk_live_"))

    def price_for(self, plan: str) -> str:
        return {"pro": self.price_pro, "business": self.price_business}.get(plan, "")

    def plan_for_price(self, price_id: str | None) -> str | None:
        mapping = {self.price_pro: "pro", self.price_business: "business"}
        mapping.pop("", None)
        return mapping.get(price_id or "")

    def client(self) -> stripe.StripeClient:
        return stripe.StripeClient(self.secret_key)


def stripe_config(db: Session) -> StripeConfig:
    """Each value comes from its environment variable when set, otherwise from the admin console."""
    s = get_settings()
    stored = {k: get_setting(db, k) for k in STRIPE_KEYS}
    secret_key = s.stripe_secret_key or stored["stripe_secret_key"]
    return StripeConfig(
        secret_key=secret_key,
        webhook_secret=s.stripe_webhook_secret or stored["stripe_webhook_secret"],
        price_pro=s.stripe_price_pro or stored["stripe_price_pro"],
        price_business=s.stripe_price_business or stored["stripe_price_business"],
        portal_config=stored["stripe_portal_config"],
        source="env" if s.stripe_secret_key else ("admin console" if secret_key else "none"),
    )


class StripeSetupError(ValueError):
    pass


def _ensure_price(client: stripe.StripeClient, plan_key: str) -> str:
    plan = PLANS[plan_key]
    lookup = f"semanticone_{plan_key}_monthly"
    existing = client.v1.prices.list(params={"lookup_keys": [lookup], "active": True, "limit": 1})
    for price in existing.data:
        if price.unit_amount == plan.monthly_price_cents and price.currency == "usd":
            return price.id
    product = client.v1.products.create(
        params={"name": f"SemanticOne {plan.name}", "description": plan.tagline, "metadata": {"plan": plan_key}}
    )
    price = client.v1.prices.create(
        params={
            "product": product.id,
            "unit_amount": plan.monthly_price_cents,
            "currency": "usd",
            "recurring": {"interval": "month"},
            "lookup_key": lookup,
            "transfer_lookup_key": True,
            "metadata": {"plan": plan_key},
        }
    )
    return price.id


def connect_stripe(db: Session, secret_key: str) -> StripeConfig:
    """Validate a Stripe key and create everything billing needs: prices, webhook, portal config."""
    secret_key = secret_key.strip()
    if not secret_key.startswith(("sk_live_", "sk_test_", "rk_live_", "rk_test_")):
        raise StripeSetupError("That doesn't look like a Stripe secret key (it should start with sk_live_ or sk_test_).")
    base_url = get_settings().base_url
    if not base_url.startswith("https://") and secret_key.startswith(("sk_live_", "rk_live_")):
        raise StripeSetupError("Set APP_BASE_URL to your public https:// address before connecting a live Stripe key.")
    client = stripe.StripeClient(secret_key)
    try:
        client.v1.balance.retrieve()
        price_pro = _ensure_price(client, "pro")
        price_business = _ensure_price(client, "business")

        webhook_url = f"{base_url}/webhooks/stripe"
        for endpoint in client.v1.webhook_endpoints.list(params={"limit": 100}).data:
            if endpoint.url == webhook_url:
                client.v1.webhook_endpoints.delete(endpoint.id)
        endpoint = client.v1.webhook_endpoints.create(
            params={
                "url": webhook_url,
                "enabled_events": WEBHOOK_EVENTS,
                "description": "SemanticOne subscriptions",
            }
        )

        portal = client.v1.billing_portal.configurations.create(
            params={
                "business_profile": {
                    "privacy_policy_url": f"{base_url}/legal/privacy",
                    "terms_of_service_url": f"{base_url}/legal/terms",
                },
                "features": {
                    "customer_update": {"enabled": True, "allowed_updates": ["email", "address", "tax_id"]},
                    "invoice_history": {"enabled": True},
                    "payment_method_update": {"enabled": True},
                    "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
                    "subscription_update": {
                        "enabled": True,
                        "default_allowed_updates": ["price"],
                        "proration_behavior": "create_prorations",
                        "products": [
                            {"product": _product_of(client, price_pro), "prices": [price_pro]},
                            {"product": _product_of(client, price_business), "prices": [price_business]},
                        ],
                    },
                },
            }
        )
    except stripe.AuthenticationError as e:
        raise StripeSetupError("Stripe rejected that key. Copy the secret key from Developers → API keys.") from e
    except stripe.PermissionError as e:
        raise StripeSetupError(
            "That restricted key lacks permissions; use a standard secret key, or allow products, prices, "
            "webhook endpoints, customers, checkout sessions, subscriptions and billing portal."
        ) from e
    except stripe.StripeError as e:
        raise StripeSetupError(f"Stripe error: {e.user_message or str(e)}") from e

    set_setting(db, "stripe_secret_key", secret_key)
    set_setting(db, "stripe_webhook_secret", endpoint.secret or "")
    set_setting(db, "stripe_price_pro", price_pro)
    set_setting(db, "stripe_price_business", price_business)
    set_setting(db, "stripe_portal_config", portal.id)
    db.commit()
    return stripe_config(db)


def _product_of(client: stripe.StripeClient, price_id: str) -> str:
    price = client.v1.prices.retrieve(price_id)
    return price.product if isinstance(price.product, str) else price.product.id


def disconnect_stripe(db: Session) -> None:
    for key in STRIPE_KEYS:
        set_setting(db, key, "")
    db.commit()
