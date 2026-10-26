from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import claim_operator, csrf_of, signup
from sqlalchemy import select

from semanticone.db import session_scope
from semanticone.models import Organization, PlatformSetting, SalesLead


@pytest.fixture(autouse=True)
def _reset_throttle():
    from semanticone.web.public import contact_throttle

    contact_throttle._failures.clear()
    yield
    contact_throttle._failures.clear()


def test_contact_form_captures_leads(client):
    token = csrf_of(client, "/contact?plan=enterprise")
    r = client.post(
        "/contact",
        data={"csrf_token": token, "name": "Dana", "email": "Dana@BigCo.com", "company": "BigCo", "plan": "enterprise", "message": "50 analysts"},
    )
    assert r.status_code == 200 and "in touch" in r.text
    # Honeypot submissions are silently dropped.
    client.post("/contact", data={"csrf_token": token, "email": "bot@spam.com", "website": "http://spam"})
    with session_scope() as db:
        leads = list(db.scalars(select(SalesLead)))
    assert [(l.email, l.company, l.plan_interest) for l in leads] == [("dana@bigco.com", "BigCo", "enterprise")]


def test_contact_form_is_rate_limited(client):
    token = csrf_of(client, "/contact")
    codes = [
        client.post("/contact", data={"csrf_token": token, "email": f"p{i}@x.com"}, follow_redirects=False).status_code
        for i in range(6)
    ]
    assert codes[:5] == [200] * 5 and codes[5] == 303


def test_upgrade_request_links_lead_to_workspace(client):
    csrf = signup(client)
    page = client.get("/app/billing").text
    assert "/contact?plan=pro&amp;workspace=acme" in page
    client.post("/contact", data={"csrf_token": csrf, "email": "owner@example.com", "plan": "pro", "workspace": "acme"})
    with session_scope() as db:
        lead = db.scalar(select(SalesLead))
        assert lead.organization is not None and lead.organization.slug == "acme"


def test_admin_console_requires_operator(client):
    csrf = signup(client, "someone@example.com", "Someone")
    assert client.get("/admin").status_code == 404
    assert client.get("/admin/workspaces").status_code == 404
    # Knowing the code is not enough without an ADMIN_EMAILS address.
    from semanticone.security import operator_claim_code

    r = client.post("/admin/claim", data={"csrf_token": csrf, "code": operator_claim_code()})
    assert r.status_code == 404


def test_admin_email_alone_does_not_grant_access(client):
    csrf = signup(client)  # owner@example.com is in ADMIN_EMAILS, but sign-up doesn't prove ownership
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/claim"
    assert 'href="/admin"' not in client.get("/app").text
    bad = client.post("/admin/claim", data={"csrf_token": csrf, "code": "AAAA-BBBB-CCCC-DDDD"})
    assert bad.status_code == 400
    assert client.get("/admin", follow_redirects=False).status_code == 303
    claim_operator(client, csrf)
    assert client.get("/admin").status_code == 200
    assert 'href="/admin"' in client.get("/app").text


def test_admin_console_sets_plans_and_tracks_leads(client):
    csrf = signup(client)  # owner@example.com is in ADMIN_EMAILS
    claim_operator(client, csrf)
    for path in ["/admin", "/admin/workspaces", "/admin/leads"]:
        assert client.get(path).status_code == 200, path
    assert 'href="/admin"' in client.get("/app").text
    with session_scope() as db:
        org_id = db.scalar(select(Organization)).id
    client.post(f"/admin/workspaces/{org_id}/plan", data={"csrf_token": csrf, "plan": "business"})
    with session_scope() as db:
        org = db.get(Organization, org_id)
        assert (org.plan, org.subscription_status) == ("business", "active")
    assert "$599" in client.get("/admin").text  # MRR estimate
    client.post("/contact", data={"csrf_token": csrf, "email": "lead@co.com"})
    with session_scope() as db:
        lead_id = db.scalar(select(SalesLead)).id
    client.post(f"/admin/leads/{lead_id}/status", data={"csrf_token": csrf, "status": "won"})
    with session_scope() as db:
        assert db.get(SalesLead, lead_id).status == "won"


class FakeStripe:
    """Records calls; mimics the parts of stripe.StripeClient().v1 that billing uses."""

    calls: list = []

    def __init__(self, key):
        self.key = key
        ns = SimpleNamespace
        rec = FakeStripe.calls

        def lister(kind):
            return lambda params=None: (rec.append((kind, params)), ns(data=[]))[1]

        def creator(kind, **extra):
            def create(params=None):
                rec.append((kind, params))
                return ns(id=f"{kind}_{len(rec)}", **extra)

            return create

        self.v1 = ns(
            balance=ns(retrieve=lambda: rec.append(("balance", key))),
            prices=ns(
                list=lister("prices.list"),
                create=creator("price"),
                retrieve=lambda pid: ns(id=pid, product=f"prod_for_{pid}"),
            ),
            products=ns(create=creator("prod")),
            webhook_endpoints=ns(list=lister("webhooks.list"), create=creator("we", secret="whsec_from_stripe"), delete=lambda i: None),
            billing_portal=ns(
                configurations=ns(create=creator("bpc")),
                sessions=ns(create=creator("bps", url="https://billing.stripe.test/portal")),
            ),
            customers=ns(create=creator("cus")),
            checkout=ns(sessions=ns(create=creator("cs", url="https://checkout.stripe.test/pay"))),
        )


def test_one_paste_stripe_connect_enables_checkout(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "")
    monkeypatch.setenv("STRIPE_PRICE_BUSINESS", "")
    from semanticone.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("semanticone.platform_settings.stripe.StripeClient", FakeStripe)
    FakeStripe.calls.clear()
    try:
        csrf = signup(client)
        claim_operator(client, csrf)
        bad = client.post("/admin/stripe/connect", data={"csrf_token": csrf, "secret_key": "pk_test_123"}, follow_redirects=True)
        assert "look like a Stripe secret key" in bad.text

        r = client.post("/admin/stripe/connect", data={"csrf_token": csrf, "secret_key": "sk_test_abc"}, follow_redirects=True)
        assert "Stripe connected in test mode" in r.text
        kinds = [c[0] for c in FakeStripe.calls]
        assert kinds.count("price") == 2 and "we" in kinds and "bpc" in kinds
        price_calls = [c[1] for c in FakeStripe.calls if c[0] == "price"]
        assert sorted(p["unit_amount"] for p in price_calls) == [14_900, 59_900]
        webhook = next(c[1] for c in FakeStripe.calls if c[0] == "we")
        assert webhook["url"] == "http://testserver/webhooks/stripe"

        with session_scope() as db:
            stored = db.get(PlatformSetting, "stripe_secret_key")
            assert stored.encrypted and "sk_test_abc" not in stored.value

        # Customers can now check out.
        r = client.post("/app/billing/checkout", data={"csrf_token": csrf, "plan": "pro"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "https://checkout.stripe.test/pay"
        checkout = next(c[1] for c in FakeStripe.calls if c[0] == "cs")
        assert checkout["line_items"][0]["price"].startswith("price_")

        # The webhook secret created in Stripe is what verifies events.
        from test_billing_and_migrations import event, signed

        with session_scope() as db:
            org_id = db.scalar(select(Organization)).id
        body, headers = signed(
            event("checkout.session.completed", {"client_reference_id": str(org_id), "customer": "cus_9", "metadata": {"plan": "pro"}}),
            secret="whsec_from_stripe",
        )
        assert client.post("/webhooks/stripe", content=body, headers=headers).status_code == 200
    finally:
        get_settings.cache_clear()


def test_operator_code_cli(capsys):
    from semanticone.cli import main
    from semanticone.security import operator_claim_code, operator_code_matches

    assert main(["operator-code"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == operator_claim_code()
    assert operator_code_matches(printed.lower().replace("-", "-"))
