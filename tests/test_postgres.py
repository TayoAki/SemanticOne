"""Integration tests against a real PostgreSQL server.

Run with, for example:
  TEST_POSTGRES_WAREHOUSE_URL=postgresql://user:pw@localhost/warehouse \
  TEST_POSTGRES_APP_URL=postgresql://user:pw@localhost/semanticone pytest tests/test_postgres.py
"""

from __future__ import annotations

import os

import duckdb
import psycopg
import pytest
from conftest import create_api_key, signup
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from semanticone.config import get_settings
from semanticone.engine.compiler import compile_query
from semanticone.engine.connectors import DemoConnector, SQLAlchemyConnector
from semanticone.engine.demo import DEMO_MODEL_YAML, ensure_demo_database
from semanticone.engine.query import parse_query
from semanticone.spec import loader

WAREHOUSE_URL = os.environ.get("TEST_POSTGRES_WAREHOUSE_URL")
APP_URL = os.environ.get("TEST_POSTGRES_APP_URL")

TABLES = ["customers", "products", "orders", "order_items", "support_tickets"]


@pytest.fixture(scope="module")
def warehouse():
    if not WAREHOUSE_URL:
        pytest.skip("TEST_POSTGRES_WAREHOUSE_URL not set")
    path = ensure_demo_database(get_settings().data_dir)
    source = duckdb.connect(str(path), read_only=True)
    with psycopg.connect(WAREHOUSE_URL, autocommit=True) as pg:
        pg.execute("DROP SCHEMA IF EXISTS shop CASCADE")
        pg.execute("CREATE SCHEMA shop")
        for table in TABLES:
            columns = source.execute(f"DESCRIBE shop.{table}").fetchall()
            ddl = ", ".join(f"{name} {dtype.replace('DOUBLE', 'DOUBLE PRECISION')}" for name, dtype, *_ in columns)
            pg.execute(f"CREATE TABLE shop.{table} ({ddl})")
            rows = source.execute(f"SELECT * FROM shop.{table}").fetchall()
            with pg.cursor().copy(f"COPY shop.{table} FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
    source.close()
    return SQLAlchemyConnector("postgres", WAREHOUSE_URL.replace("postgresql://", "postgresql+psycopg://", 1))


QUERIES = [
    {"metrics": ["total_revenue", "order_count", "ticket_count"], "dimensions": ["customers.country"]},
    {"metrics": ["order_count", "average_order_value"], "dimensions": ["products.category"]},
    {"metrics": ["total_revenue"], "dimensions": ["orders.ordered_at__month"], "filters": [{"field": "orders.channel", "op": "in", "value": ["web", "mobile"]}]},
    {"metrics": ["return_rate", "avg_satisfaction", "customer_count"], "dimensions": ["customers.segment"]},
    {"metrics": ["orders.shipping_revenue", "order_items.units_sold"]},
]


@pytest.mark.parametrize("query", QUERIES)
def test_postgres_matches_duckdb(warehouse, query):
    model = loader.load(DEMO_MODEL_YAML).model()
    q = parse_query({**query, "limit": 1000})
    pg_rows = warehouse.execute(compile_query(model, q, "postgres").sql).rows
    duck_rows = DemoConnector().execute(compile_query(model, q, "duckdb").sql).rows
    assert len(pg_rows) == len(duck_rows)
    n_dims = len(query.get("dimensions", []))
    pg = {tuple(str(v)[:10] for v in r[:n_dims]): r[n_dims:] for r in pg_rows}
    duck = {tuple(str(v)[:10] for v in r[:n_dims]): r[n_dims:] for r in duck_rows}
    assert pg.keys() == duck.keys()
    for key in duck:
        for a, b in zip(pg[key], duck[key]):
            assert a == pytest.approx(b, rel=1e-6, abs=1e-6)


def test_full_app_on_postgres(warehouse, monkeypatch):
    if not APP_URL:
        pytest.skip("TEST_POSTGRES_APP_URL not set")
    engine = create_engine(APP_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    monkeypatch.setattr("semanticone.engine.connectors._check_public_host", lambda host, port: None)

    from semanticone.main import create_app

    with TestClient(create_app(APP_URL)) as client:
        csrf = signup(client)
        from semanticone.db import session_scope
        from semanticone.models import Organization

        with session_scope() as db:
            org = db.query(Organization).one()
            org.plan, org.subscription_status = "pro", "active"
        r = client.post(
            "/app/connections",
            data={"csrf_token": csrf, "name": "pg", "kind": "postgres", "url": WAREHOUSE_URL},
            follow_redirects=True,
        )
        assert "Connected successfully" in r.text, r.text[:2000]
        key = create_api_key(client, csrf)
        created = client.post(
            "/api/v1/models",
            json={"content": DEMO_MODEL_YAML.replace("name: Acme Commerce", "name: On Postgres"), "connection": "pg"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert created.status_code == 201, created.text
        result = client.post(
            "/api/v1/models/on-postgres/query",
            json={"metrics": ["total_revenue"], "dimensions": ["orders.ordered_at__year"]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert result.status_code == 200, result.text
        assert len(result.json()["rows"]) == 3
