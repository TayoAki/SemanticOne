from __future__ import annotations

import pytest
from conftest import TESTS_DIR

from semanticone.engine.compiler import compile_query
from semanticone.engine.connectors import DemoConnector
from semanticone.engine.demo import DEMO_MODEL_YAML
from semanticone.engine.query import QueryError, parse_query
from semanticone.spec import loader

MODEL = loader.load(DEMO_MODEL_YAML).model()
TPCDS = loader.load((TESTS_DIR / "tpcds_semantic_model.yaml").read_text()).model()


@pytest.fixture(scope="module")
def warehouse():
    return DemoConnector()


def run(warehouse, query: dict) -> dict:
    compiled = compile_query(MODEL, parse_query(query), "duckdb")
    result = warehouse.execute(compiled.sql)
    return {tuple(r[: len(query.get("dimensions", []))]): r[len(query.get("dimensions", [])) :] for r in result.rows}


def truth(warehouse, sql: str) -> dict:
    rows = warehouse.execute(sql).rows
    return {tuple(r[:-1]): r[-1] for r in rows}


def approx_equal(a, b):
    return a == pytest.approx(b, rel=1e-9) if isinstance(a, float) or isinstance(b, float) else a == b


def test_revenue_by_country_matches_hand_written_sql(warehouse):
    got = run(warehouse, {"metrics": ["total_revenue"], "dimensions": ["customers.country"]})
    expected = truth(
        warehouse,
        """SELECT c.country, SUM(CASE WHEN o.status <> 'cancelled' THEN i.quantity * i.unit_price * (1 - i.discount_pct) ELSE 0 END)
           FROM shop.order_items i JOIN shop.orders o USING (order_id) JOIN shop.customers c USING (customer_id)
           GROUP BY 1""",
    )
    assert set(got) == set(expected)
    for key, value in expected.items():
        assert approx_equal(got[key][0], value)


def test_metrics_from_three_fact_tables_do_not_fan_out(warehouse):
    got = run(warehouse, {"metrics": ["order_count", "ticket_count", "total_revenue"], "dimensions": ["customers.country"]})
    orders = truth(
        warehouse,
        "SELECT c.country, COUNT(DISTINCT o.order_id) FILTER (WHERE o.status <> 'cancelled') "
        "FROM shop.orders o JOIN shop.customers c USING (customer_id) GROUP BY 1",
    )
    tickets = truth(
        warehouse,
        "SELECT c.country, COUNT(*) FROM shop.support_tickets t JOIN shop.customers c USING (customer_id) GROUP BY 1",
    )
    for key in orders:
        assert got[key][0] == orders[key]
        assert got[key][1] == tickets.get(key, 0) or (got[key][1] is None and key not in tickets)
    # A naive join of orders x tickets would inflate ticket counts; make sure we didn't.
    assert sum(v[1] or 0 for v in got.values()) == warehouse.execute("SELECT COUNT(*) FROM shop.support_tickets").rows[0][0]


def test_distinct_count_rooted_through_bridge(warehouse):
    got = run(warehouse, {"metrics": ["order_count"], "dimensions": ["products.category"]})
    expected = truth(
        warehouse,
        """SELECT p.category, COUNT(DISTINCT CASE WHEN o.status <> 'cancelled' THEN o.order_id END)
           FROM shop.order_items i JOIN shop.orders o USING (order_id) JOIN shop.products p USING (product_id)
           GROUP BY 1""",
    )
    assert {k: v[0] for k, v in got.items()} == expected


def test_grand_total_and_ratio(warehouse):
    got = run(warehouse, {"metrics": ["customer_count", "average_order_value"]})
    (customers, aov), = got.values()
    assert customers == 1200
    revenue = warehouse.execute(
        "SELECT SUM(i.quantity * i.unit_price * (1 - i.discount_pct)) FROM shop.order_items i "
        "JOIN shop.orders o USING (order_id) WHERE o.status <> 'cancelled'"
    ).rows[0][0]
    orders = warehouse.execute("SELECT COUNT(*) FROM shop.orders WHERE status <> 'cancelled'").rows[0][0]
    assert aov == pytest.approx(revenue / orders)


def test_unsafe_question_is_refused():
    with pytest.raises(QueryError, match="double count"):
        compile_query(MODEL, parse_query({"metrics": ["ticket_count"], "dimensions": ["products.category"]}), "duckdb")


def test_time_grain_filters_and_metric_filter(warehouse):
    got = run(
        warehouse,
        {
            "metrics": ["total_revenue", "order_count"],
            "dimensions": ["orders.ordered_at__year", "orders.channel"],
            "filters": [
                {"field": "orders.ordered_at", "op": "between", "value": ["2025-01-01", "2025-12-31 23:59:59"]},
                {"field": "customers.country", "op": "in", "value": ["US", "GB"]},
                {"field": "order_count", "op": ">", "value": 50},
            ],
        },
    )
    assert got
    assert all(key[0].startswith("2025-01-01") for key in got)
    assert all(values[1] > 50 for values in got.values())


def test_filter_values_are_literals_not_sql(warehouse):
    got = run(
        warehouse,
        {"metrics": ["order_count"], "filters": [{"field": "customers.country", "op": "=", "value": "US' OR '1'='1"}]},
    )
    assert list(got.values())[0][0] == 0


def test_dimension_only_query(warehouse):
    got = run(warehouse, {"dimensions": ["customers.segment"]})
    assert set(k[0] for k in got) == {"Consumer", "Small Business", "Enterprise"}


def test_dataset_scoped_metrics(warehouse):
    got = run(warehouse, {"metrics": ["order_items.units_sold"], "dimensions": ["products.category"]})
    expected = truth(
        warehouse,
        "SELECT p.category, SUM(i.quantity) FROM shop.order_items i JOIN shop.products p USING (product_id) GROUP BY 1",
    )
    assert {k: v[0] for k, v in got.items()} == expected


def test_unknown_names_suggest_alternatives():
    with pytest.raises(QueryError, match="Did you mean `total_revenue`"):
        compile_query(MODEL, parse_query({"metrics": ["total_revenu"]}), "duckdb")
    with pytest.raises(QueryError, match="Unknown field"):
        compile_query(MODEL, parse_query({"metrics": ["total_revenue"], "dimensions": ["orders.nope"]}), "duckdb")


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "snowflake", "bigquery", "databricks", "mysql", "redshift", "tsql"])
def test_compiles_for_every_supported_dialect(dialect):
    compiled = compile_query(
        MODEL,
        parse_query(
            {
                "metrics": ["total_revenue", "order_count"],
                "dimensions": ["orders.ordered_at__month", "customers.country"],
                "filters": [{"field": "customers.segment", "op": "!=", "value": "Enterprise"}],
            }
        ),
        dialect,
    )
    assert "SELECT" in compiled.sql
    assert compiled.columns == ["orders.ordered_at__month", "customers.country", "total_revenue", "order_count"]


def test_window_metrics_require_their_grouping():
    ok = compile_query(TPCDS, parse_query({"metrics": ["cumulative_sales"], "dimensions": ["date_dim.d_date"]}), "duckdb")
    assert "OVER" in ok.sql
    with pytest.raises(QueryError, match="requires grouping by `date_dim.d_date`"):
        compile_query(TPCDS, parse_query({"metrics": ["cumulative_sales"], "dimensions": ["date_dim.d_year"]}), "duckdb")


def test_ratio_of_two_datasets_in_tpcds():
    compiled = compile_query(TPCDS, parse_query({"metrics": ["customer_lifetime_value"]}), "snowflake")
    roots = {p["root"] for p in compiled.plan}
    assert roots == {"store_sales", "customer"}


def test_parse_query_validation():
    with pytest.raises(QueryError):
        parse_query({})
    with pytest.raises(QueryError):
        parse_query({"metrics": ["x"], "filters": [{"field": "a.b", "op": "drop"}]})
    with pytest.raises(QueryError):
        parse_query({"metrics": ["x"], "dimensions": [{"field": "a.b", "grain": "fortnight"}]})
    assert parse_query({"metrics": ["x"], "limit": 10_000}, max_rows=500).limit == 500
