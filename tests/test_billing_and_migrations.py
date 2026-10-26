from __future__ import annotations

import hashlib
import hmac
import json
import time

from conftest import signup
from sqlalchemy import create_engine, select

from semanticone.db import Base, session_scope
from semanticone.migrate import upgrade_database
from semanticone.models import Organization
from semanticone.services import plan_for


def signed(payload: dict, secret: str = "whsec_test") -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"}


def event(kind: str, obj: dict) -> dict:
    return {"id": "evt_1", "object": "event", "type": kind, "data": {"object": obj}}


def org_state():
    with session_scope() as db:
        org = db.scalar(select(Organization))
        return org.plan, org.subscription_status, org.stripe_customer_id, plan_for(org).key


def test_webhook_rejects_bad_signature(client):
    body, headers = signed(event("checkout.session.completed", {}), secret="wrong")
    assert client.post("/webhooks/stripe", content=body, headers=headers).status_code == 400


def test_subscription_lifecycle(client):
    signup(client)
    with session_scope() as db:
        org_id = db.scalar(select(Organization)).id

    body, headers = signed(
        event(
            "checkout.session.completed",
            {"object": "checkout.session", "client_reference_id": str(org_id), "customer": "cus_1", "subscription": None, "metadata": {"org_id": str(org_id), "plan": "pro"}},
        )
    )
    assert client.post("/webhooks/stripe", content=body, headers=headers).status_code == 200
    assert org_state() == ("pro", "active", "cus_1", "pro")

    sub = {
        "object": "subscription",
        "id": "sub_1",
        "customer": "cus_1",
        "status": "active",
        "items": {"data": [{"price": {"id": "price_business_test"}}]},
        "metadata": {},
    }
    body, headers = signed(event("customer.subscription.updated", sub))
    client.post("/webhooks/stripe", content=body, headers=headers)
    assert org_state()[0] == "business"

    body, headers = signed(event("invoice.payment_failed", {"object": "invoice", "customer": "cus_1"}))
    client.post("/webhooks/stripe", content=body, headers=headers)
    assert org_state()[1] == "past_due" and org_state()[3] == "business"

    body, headers = signed(event("customer.subscription.deleted", {**sub, "status": "canceled"}))
    client.post("/webhooks/stripe", content=body, headers=headers)
    assert org_state()[0] == "free" and org_state()[3] == "free"


def test_lapsed_paid_plan_falls_back_to_free_limits():
    org = Organization(name="x", slug="x", plan="pro", subscription_status="unpaid")
    assert plan_for(org).key == "free"
    org.subscription_status = "active"
    assert plan_for(org).key == "pro"
    enterprise = Organization(name="e", slug="e", plan="enterprise", subscription_status=None)
    assert plan_for(enterprise).key == "enterprise"


def test_billing_page_without_stripe_offers_sales(client):
    signup(client)
    page = client.get("/app/billing").text
    assert "Contact sales" in page


def test_migrations_match_models(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    upgrade_database(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migrations differ; run `alembic revision --autogenerate`: {diff}"


def test_instance_keys_generated_once_and_private(tmp_path):
    import stat

    from cryptography.fernet import Fernet

    from semanticone.config import INSTANCE_KEYS_FILE, _instance_keys

    first = _instance_keys(tmp_path)
    second = _instance_keys(tmp_path)
    assert first == second
    Fernet(first["encryption_key"].encode())  # valid Fernet key
    assert len(first["secret_key"]) >= 48
    mode = stat.S_IMODE((tmp_path / INSTANCE_KEYS_FILE).stat().st_mode)
    assert mode == 0o600
    assert list(p.name for p in tmp_path.iterdir()) == [INSTANCE_KEYS_FILE]
