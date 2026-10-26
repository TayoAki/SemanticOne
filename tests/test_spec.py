from __future__ import annotations

from conftest import TESTS_DIR

from semanticone.engine.demo import DEMO_MODEL_YAML
from semanticone.spec import loader


def messages(result):
    return [f"{i.path}: {i.message}" for i in result.errors]


def test_official_tpcds_example_is_valid():
    result = loader.load((TESTS_DIR / "tpcds_semantic_model.yaml").read_text())
    assert result.errors == []
    model = result.model()
    assert set(model.datasets) == {"store_sales", "date_dim", "customer", "item", "store"}
    assert "customer_lifetime_value" in model.all_metrics()
    assert "store_sales.total_quantity" in model.all_metrics()
    # Window metrics are allowed but carry a grouping requirement.
    assert any("cumulative_sales" in w.path for w in result.warnings)


def test_sample_model_is_clean():
    result = loader.load(DEMO_MODEL_YAML)
    assert result.issues == []


def test_legacy_wrapper_is_migrated():
    legacy = """
version: 0.1.1
semantic_model:
  - name: legacy
    datasets:
      - name: t
        source: db.s.t
        fields:
          - name: x
            expression: {dialects: [{dialect: ANSI_SQL, expression: x}]}
"""
    result = loader.load(legacy)
    assert result.ok
    assert result.document["version"] == "0.2.0.dev0"
    assert result.document["name"] == "legacy"
    assert result.notes


def test_legacy_multi_model_is_not_silently_truncated():
    legacy = """
semantic_model:
  - {name: a, datasets: [{name: t, source: s.t}]}
  - {name: b, datasets: [{name: t, source: s.t}]}
"""
    result = loader.load(legacy)
    assert not result.ok
    assert len(result.documents) == 2
    assert "2 models" in result.errors[0].message


def test_duplicate_yaml_keys_rejected():
    result = loader.load("version: 0.2.0.dev0\nname: a\nname: b\ndatasets: []\n")
    assert "Duplicate key" in result.errors[0].message


def test_schema_errors_reported_with_paths():
    result = loader.load("version: 0.2.0.dev0\nname: x\ndatasets: [{name: t}]\n")
    assert any(i.path.startswith("datasets") for i in result.errors)


def test_reference_errors():
    doc = """
version: 0.2.0.dev0
name: refs
datasets:
  - name: orders
    source: s.orders
    primary_key: [missing_pk]
    fields:
      - {name: id, expression: {sql: id}}
      - {name: customer_id, expression: {sql: customer_id}}
      - {name: bad, expression: {sql: SUM(x)}}
    metrics:
      - {name: cross, expression: {sql: SUM(customers.id)}}
  - name: customers
    source: s.customers
    fields:
      - {name: id, expression: {sql: id}}
relationships:
  - {name: r, from: orders, to: customers, from_columns: [customer_id], to_columns: [nope]}
metrics:
  - {name: unqualified, expression: {sql: SUM(id)}}
  - {name: unknown_field, expression: {sql: SUM(orders.amount)}}
"""
    errors = "\n".join(messages(loader.load(doc)))
    assert "missing_pk" in errors
    assert "row-level" in errors
    assert "dataset-scoped metrics can only reference fields of `orders`" in errors
    assert "`nope` is not a field of `customers`" in errors
    assert "must be qualified" in errors
    assert "`amount` is not a field of `orders`" in errors


def test_relationship_to_non_key_warns():
    doc = """
version: 0.2.0.dev0
name: w
datasets:
  - {name: a, source: s.a, fields: [{name: b_id, expression: {sql: b_id}}]}
  - {name: b, source: s.b, fields: [{name: id, expression: {sql: id}}]}
relationships:
  - {name: a_b, from: a, to: b, from_columns: [b_id], to_columns: [id]}
"""
    result = loader.load(doc)
    assert result.errors == []
    assert any("not a declared primary or unique key" in w.message for w in result.warnings)


def test_statements_in_expressions_rejected():
    doc = """
version: 0.2.0.dev0
name: s
datasets:
  - name: t
    source: s.t
    fields:
      - {name: x, expression: {sql: "(SELECT secret FROM other)"}}
"""
    result = loader.load(doc)
    assert any("cannot be queried" in w.message for w in result.warnings)
