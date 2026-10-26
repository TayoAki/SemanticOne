from __future__ import annotations

from conftest import create_api_key, signup
from sqlalchemy import select

from semanticone.db import session_scope
from semanticone.engine.demo import DEMO_MODEL_YAML
from semanticone.models import Organization


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def test_api_requires_valid_key(client):
    assert client.get("/api/v1/models").status_code == 401
    assert client.get("/api/v1/models", headers=auth("so_live_nope")).status_code == 401


def test_api_end_to_end(client):
    csrf = signup(client)
    key = create_api_key(client, csrf)
    client.cookies.clear()
    h = auth(key)

    models = client.get("/api/v1/models", headers=h).json()["models"]
    assert [m["slug"] for m in models] == ["acme-commerce"]

    described = client.get("/api/v1/models/acme-commerce", headers=h).json()
    names = [m["name"] for m in described["catalog"]["metrics"]]
    assert names[0] == "total_revenue"
    assert described["catalog"]["metrics"][0]["default_time_dimension"] == "orders.ordered_at"

    q = {"metrics": ["total_revenue", "order_count"], "dimensions": ["customers.segment"], "order_by": ["-total_revenue"]}
    compiled = client.post("/api/v1/models/acme-commerce/compile", json=q, headers=h).json()
    assert "SELECT" in compiled["sql"]
    result = client.post("/api/v1/models/acme-commerce/query", json=q, headers=h).json()
    assert result["columns"] == ["customers.segment", "total_revenue", "order_count"]
    assert result["rows"][0][1] >= result["rows"][-1][1]

    bad = client.post("/api/v1/models/acme-commerce/query", json={"metrics": ["ticket_count"], "dimensions": ["products.category"]}, headers=h)
    assert bad.status_code == 400 and "double count" in bad.json()["error"]["message"]

    usage = client.get("/api/v1/usage", headers=h).json()
    assert usage["plan"] == "free" and usage["usage"]["queries"] == 1

    doc = client.get("/api/v1/models/acme-commerce/document", headers=h).text
    put = client.put(
        "/api/v1/models/acme-commerce/document",
        content=doc.replace("Sample e-commerce model", "Edited via CI"),
        headers={**h, "Content-Type": "application/yaml", "X-Change-Message": "from CI"},
    )
    assert put.status_code == 200 and put.json()["changed"] is True
    assert put.json()["model"]["version"] == 2
    same = client.put("/api/v1/models/acme-commerce/document", json={"content": client.get("/api/v1/models/acme-commerce/document", headers=h).text}, headers=h)
    assert same.json()["changed"] is False

    invalid = client.put("/api/v1/models/acme-commerce/document", content="name: x", headers={**h, "Content-Type": "application/yaml"})
    assert invalid.status_code == 422 and invalid.json()["error"]["details"]

    validated = client.post("/api/v1/validate", content=DEMO_MODEL_YAML, headers={**h, "Content-Type": "application/yaml"}).json()
    assert validated["valid"] is True

    created = client.post(
        "/api/v1/models",
        json={"content": DEMO_MODEL_YAML.replace("name: Acme Commerce", "name: From API"), "connection": "Sample warehouse"},
        headers=h,
    )
    assert created.status_code == 201
    assert created.json()["model"]["connection"]["kind"] == "demo"


def test_monthly_quota_enforced(client):
    csrf = signup(client)
    key = create_api_key(client, csrf)
    with session_scope() as db:
        org = db.scalar(select(Organization))
        org.plan = "free"
    from semanticone import plans

    original = plans.PLANS["free"]
    plans.PLANS["free"] = plans.Plan(**{**original.__dict__, "monthly_queries": 1})
    try:
        q = {"metrics": ["order_count"]}
        assert client.post("/api/v1/models/acme-commerce/query", json=q, headers=auth(key)).status_code == 200
        r = client.post("/api/v1/models/acme-commerce/query", json=q, headers=auth(key))
        assert r.status_code == 402 and "Upgrade" in r.json()["error"]["message"]
    finally:
        plans.PLANS["free"] = original


def test_revoked_key_stops_working(client):
    csrf = signup(client)
    key = create_api_key(client, csrf)
    assert client.get("/api/v1/models", headers=auth(key)).status_code == 200
    client.post("/app/api-keys/1/revoke", data={"csrf_token": csrf})
    assert client.get("/api/v1/models", headers=auth(key)).status_code == 401


def rpc(client, key, method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "method": method, "id": id_}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers={**auth(key), "Accept": "application/json, text/event-stream"})


def test_mcp_server(client):
    csrf = signup(client)
    key = create_api_key(client, csrf)
    client.cookies.clear()

    assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1}).status_code == 401
    assert client.get("/mcp", headers=auth(key)).status_code == 405

    init = rpc(client, key, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}).json()
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in init["result"]["capabilities"]
    note = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=auth(key))
    assert note.status_code == 202

    tools = rpc(client, key, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == {"list_models", "describe_model", "query_metrics", "compile_sql", "validate_model"}

    listed = rpc(client, key, "tools/call", {"name": "list_models", "arguments": {}}).json()["result"]
    assert listed["structuredContent"]["models"][0]["model"] == "acme-commerce"

    described = rpc(client, key, "tools/call", {"name": "describe_model", "arguments": {"model": "acme-commerce"}}).json()["result"]
    assert any(m["name"] == "avg_satisfaction" for m in described["structuredContent"]["metrics"])

    queried = rpc(
        client,
        key,
        "tools/call",
        {"name": "query_metrics", "arguments": {"model": "acme-commerce", "metrics": ["ticket_count"], "dimensions": ["support_tickets.priority"]}},
    ).json()["result"]
    assert queried["isError"] is False
    assert queried["structuredContent"]["row_count"] == 4

    refused = rpc(
        client,
        key,
        "tools/call",
        {"name": "query_metrics", "arguments": {"model": "acme-commerce", "metrics": ["ticket_count"], "dimensions": ["products.category"]}},
    ).json()["result"]
    assert refused["isError"] is True and "double count" in refused["content"][0]["text"]

    validated = rpc(client, key, "tools/call", {"name": "validate_model", "arguments": {"content": "name: x"}}).json()["result"]
    assert validated["structuredContent"]["valid"] is False

    assert rpc(client, key, "tools/call", {"name": "nope", "arguments": {}}).json()["error"]["code"] == -32602
    assert rpc(client, key, "does/not/exist").json()["error"]["code"] == -32601


def test_compile_only_models_choose_a_known_dialect(client):
    csrf = signup(client)
    key = create_api_key(client, csrf)
    h = auth(key)
    created = client.post("/api/v1/models", json={"content": DEMO_MODEL_YAML.replace("name: Acme Commerce", "name: No Conn")}, headers=h)
    assert created.status_code == 201 and created.json()["model"]["connection"] is None
    ok = client.post("/api/v1/models/no-conn/compile", json={"metrics": ["total_revenue"], "dialect": "bigquery"}, headers=h)
    assert ok.status_code == 200 and "`" in ok.json()["sql"]
    bad = client.post("/api/v1/models/no-conn/compile", json={"metrics": ["total_revenue"], "dialect": "oracle9i"}, headers=h)
    assert bad.status_code == 400
    run = client.post("/api/v1/models/no-conn/query", json={"metrics": ["total_revenue"]}, headers=h)
    assert run.status_code == 409 and "connection" in run.json()["error"]["message"]
