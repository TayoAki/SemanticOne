from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_DATA = Path(tempfile.mkdtemp(prefix="semanticone-tests-"))
os.environ.setdefault("DATA_DIR", str(_DATA))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["APP_BASE_URL"] = "http://testserver"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
os.environ["ADMIN_EMAILS"] = "owner@example.com"
os.environ["STRIPE_PRICE_PRO"] = "price_pro_test"
os.environ["STRIPE_PRICE_BUSINESS"] = "price_business_test"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from semanticone.config import get_settings  # noqa: E402

get_settings.cache_clear()

from semanticone.main import create_app  # noqa: E402

PASSWORD = "correct horse battery"
TESTS_DIR = Path(__file__).parent


@pytest.fixture
def client():
    app = create_app("sqlite://")
    with TestClient(app) as c:
        yield c


def csrf_of(client: TestClient, path: str = "/app") -> str:
    html = client.get(path).text
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def signup(client: TestClient, email: str = "owner@example.com", workspace: str = "Acme") -> str:
    token = csrf_of(client, "/signup")
    r = client.post(
        "/signup",
        data={"csrf_token": token, "name": "Owner Person", "email": email, "password": PASSWORD, "workspace": workspace},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return csrf_of(client)


def create_api_key(client: TestClient, csrf: str, name: str = "test key") -> str:
    r = client.post("/app/api-keys", data={"csrf_token": csrf, "name": name}, follow_redirects=True)
    assert r.status_code == 200
    match = re.search(r'id="new-key">([^<]+)<', r.text)
    assert match, "API key not displayed"
    return match.group(1).strip()


def set_plan(slug_or_email: str, plan: str) -> None:
    from sqlalchemy import select

    from semanticone.db import session_scope
    from semanticone.models import Membership, Organization, User

    with session_scope() as db:
        org = db.scalar(select(Organization).where(Organization.slug == slug_or_email))
        if org is None:
            user = db.scalar(select(User).where(User.email == slug_or_email))
            membership = db.scalar(select(Membership).where(Membership.user_id == user.id))
            org = membership.organization
        org.plan = plan
        org.subscription_status = "active"


def claim_operator(client: TestClient, csrf: str) -> None:
    from semanticone.security import operator_claim_code

    r = client.post("/admin/claim", data={"csrf_token": csrf, "code": operator_claim_code()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin", r.text[:300]
