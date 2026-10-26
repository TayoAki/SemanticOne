from __future__ import annotations

import re

from conftest import PASSWORD, csrf_of, set_plan, signup
from sqlalchemy import select

from semanticone.db import session_scope
from semanticone.engine.demo import DEMO_MODEL_YAML
from semanticone.models import Connection


def test_public_pages(client):
    for path in ["/", "/pricing", "/docs", "/legal/terms", "/legal/privacy", "/login", "/signup", "/api/docs", "/healthz"]:
        assert client.get(path).status_code == 200, path


def test_app_requires_login(client):
    r = client.get("/app/models/x", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=")


def test_signup_provisions_sample_model_and_pages_render(client):
    signup(client)
    for path in [
        "/app",
        "/app/models/acme-commerce",
        "/app/models/acme-commerce/explore",
        "/app/models/acme-commerce/edit",
        "/app/models/acme-commerce/versions",
        "/app/models/acme-commerce/versions/1",
        "/app/models/acme-commerce/settings",
        "/app/models/new",
        "/app/connections",
        "/app/api-keys",
        "/app/usage",
        "/app/settings",
        "/app/billing",
    ]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:500])
    assert "Acme Commerce" in client.get("/app").text


def test_query_from_explorer_and_csrf(client):
    csrf = signup(client)
    body = {"metrics": ["total_revenue"], "dimensions": ["orders.channel"]}
    assert client.post("/app/models/acme-commerce/query", json=body).status_code == 403
    r = client.post("/app/models/acme-commerce/query", json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["columns"] == ["orders.channel", "total_revenue"]
    assert len(data["rows"]) == 4
    assert "Queries this month" in client.get("/app/usage").text
    bad = client.post("/app/models/acme-commerce/query", json={"metrics": ["nope"]}, headers={"X-CSRF-Token": csrf})
    assert bad.status_code == 400 and "Unknown metric" in bad.json()["error"]


def test_login_logout(client):
    csrf = signup(client)
    client.post("/logout", data={"csrf_token": csrf})
    assert client.get("/app", follow_redirects=False).status_code == 303
    token = csrf_of(client, "/login")
    bad = client.post("/login", data={"csrf_token": token, "email": "owner@example.com", "password": "wrong password"})
    assert bad.status_code == 400
    ok = client.post(
        "/login", data={"csrf_token": token, "email": "owner@example.com", "password": PASSWORD}, follow_redirects=False
    )
    assert ok.status_code == 303
    assert client.get("/app").status_code == 200


def test_edit_versions_diff_restore_download(client):
    csrf = signup(client)
    original = client.get("/app/models/acme-commerce/download").text
    edited = original.replace("name: Acme Commerce", "name: Acme Commerce Edited")
    r = client.post(
        "/app/models/acme-commerce/edit", data={"csrf_token": csrf, "content": edited, "message": "rename"}
    )
    assert r.status_code == 200
    assert "Acme Commerce Edited" in client.get("/app/models/acme-commerce").text
    diff = client.get("/app/models/acme-commerce/versions/2").text
    assert "Acme Commerce Edited" in diff and "rename" in diff
    invalid = client.post(
        "/app/models/acme-commerce/edit", data={"csrf_token": csrf, "content": "version: 0.2.0.dev0\nname: x\n"}
    )
    assert invalid.status_code == 422
    client.post("/app/models/acme-commerce/versions/1/restore", data={"csrf_token": csrf})
    assert client.get("/app/models/acme-commerce/download").text == original
    as_json = client.get("/app/models/acme-commerce/download?format=json")
    assert as_json.json()["name"] == "Acme Commerce"


def test_live_validation_endpoint(client):
    csrf = signup(client)
    r = client.post(
        "/app/models/acme-commerce/validate", json={"content": "name: x"}, headers={"X-CSRF-Token": csrf}
    )
    assert r.json()["ok"] is False


def test_free_plan_model_limit(client):
    csrf = signup(client)
    doc = DEMO_MODEL_YAML.replace("name: Acme Commerce", "name: Second")
    r = client.post("/app/models/new", data={"csrf_token": csrf, "content": doc}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/models/second"
    doc3 = DEMO_MODEL_YAML.replace("name: Acme Commerce", "name: Third")
    r = client.post("/app/models/new", data={"csrf_token": csrf, "content": doc3}, follow_redirects=False)
    assert r.headers["location"] == "/app/billing"


def test_upload_legacy_file_is_migrated(client):
    csrf = signup(client)
    legacy = "version: 0.1.1\nsemantic_model:\n  - name: Legacy\n    datasets:\n      - name: t\n        source: s.t\n"
    r = client.post(
        "/app/models/new",
        data={"csrf_token": csrf, "content": ""},
        files={"file": ("legacy.yaml", legacy.encode(), "application/yaml")},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/app/models/legacy"
    assert "version: 0.2.0.dev0" in client.get("/app/models/legacy/download").text


def test_warehouse_connections_require_paid_plan_and_block_private_hosts(client):
    csrf = signup(client)
    data = {"csrf_token": csrf, "name": "prod", "kind": "postgres", "url": "postgresql://u:secretpw@127.0.0.1:5432/db"}
    r = client.post("/app/connections", data=data, follow_redirects=True)
    assert "paid plans" in r.text
    set_plan("owner@example.com", "pro")
    r = client.post("/app/connections", data=data, follow_redirects=True)
    assert "private address" in r.text
    with session_scope() as db:
        assert db.scalar(select(Connection).where(Connection.name == "prod")) is None


def test_connection_credentials_are_encrypted(client, monkeypatch):
    monkeypatch.setattr("semanticone.engine.connectors._check_public_host", lambda host, port: None)
    csrf = signup(client)
    set_plan("owner@example.com", "pro")
    client.post(
        "/app/connections",
        data={"csrf_token": csrf, "name": "prod", "kind": "postgres", "url": "postgresql://u:secretpw@db.example.com:5432/db"},
    )
    with session_scope() as db:
        conn = db.scalar(select(Connection).where(Connection.name == "prod"))
        assert conn is not None
        assert "secretpw" not in conn.encrypted_url
    page = client.get("/app/connections").text
    assert "secretpw" not in page and "db.example.com" in page


def test_workspaces_are_isolated(client):
    signup(client, "alice@example.com", "Alice Co")
    client.cookies.clear()
    csrf = signup(client, "bob@example.com", "Bob Co")
    # Bob's workspace also has a model with the same slug; make sure he sees his own and cannot reach Alice's by id.
    assert client.get("/app/models/acme-commerce").status_code == 200
    with session_scope() as db:
        alice_conn = db.scalar(select(Connection).join(Connection.organization).where(Connection.organization.has(slug="alice-co")))
        alice_conn_id = alice_conn.id
    r = client.post(f"/app/connections/{alice_conn_id}/delete", data={"csrf_token": csrf})
    assert r.status_code == 404
    r = client.post(
        "/app/models/acme-commerce/settings", data={"csrf_token": csrf, "connection_id": str(alice_conn_id)}
    )
    assert r.status_code == 404


def test_invites(client):
    csrf = signup(client)
    r = client.post("/app/settings/invites", data={"csrf_token": csrf, "email": "teammate@example.com", "role": "member"}, follow_redirects=True)
    link = re.search(r"http://testserver/invite/([\w-]+)", r.text)
    assert link
    client.cookies.clear()
    r = client.get(f"/invite/{link.group(1)}", follow_redirects=False)
    assert r.headers["location"] == f"/signup?invite={link.group(1)}"
    token = csrf_of(client, f"/signup?invite={link.group(1)}")
    r = client.post(
        "/signup",
        data={"csrf_token": token, "email": "teammate@example.com", "password": PASSWORD, "invite_token": link.group(1)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/app").text
    assert "Acme" in page
    assert client.get("/app/settings").text.count("teammate@example.com") == 1
    # Members cannot create API keys.
    member_csrf = csrf_of(client)
    assert client.post("/app/api-keys", data={"csrf_token": member_csrf, "name": "x"}).status_code == 403


def test_password_reset(client, monkeypatch):
    sent = {}
    monkeypatch.setattr("semanticone.web.public.send_email", lambda to, subject, body: sent.update(body=body) or True)
    signup(client)
    client.cookies.clear()
    token = csrf_of(client, "/forgot")
    client.post("/forgot", data={"csrf_token": token, "email": "owner@example.com"})
    link = re.search(r"/reset/(\S+)", sent["body"]).group(1)
    token = csrf_of(client, f"/reset/{link}")
    r = client.post(f"/reset/{link}", data={"csrf_token": token, "password": "a brand new password"}, follow_redirects=False)
    assert r.headers["location"] == "/login"
    # The link is single-use.
    r = client.post(f"/reset/{link}", data={"csrf_token": token, "password": "another new password"}, follow_redirects=False)
    assert r.headers["location"] == "/forgot"
    token = csrf_of(client, "/login")
    ok = client.post("/login", data={"csrf_token": token, "email": "owner@example.com", "password": "a brand new password"}, follow_redirects=False)
    assert ok.status_code == 303


def test_login_throttle(client):
    signup(client)
    client.cookies.clear()
    token = csrf_of(client, "/login")
    codes = [
        client.post("/login", data={"csrf_token": token, "email": "owner@example.com", "password": "wrong password"}).status_code
        for _ in range(9)
    ]
    assert codes[:8] == [400] * 8 and codes[8] == 429
    # Even the right password is refused while throttled.
    blocked = client.post("/login", data={"csrf_token": token, "email": "owner@example.com", "password": PASSWORD})
    assert blocked.status_code == 429
    from semanticone.security import login_throttle

    login_throttle._failures.clear()
