"""Typed, read-only view of an Ossie semantic model document."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SPEC_VERSION = "0.2.0.dev0"
TEMPORAL_TYPES = {"Date", "Time", "DateTime", "DateTimeTz"}

# Ossie dialect tokens -> sqlglot dialect names. None means sqlglot's generic (ANSI-ish) dialect.
PORTABLE_DIALECTS = ("OSSIE_SQL_2026", "ANSI_SQL")
DIALECT_TO_SQLGLOT: dict[str, str | None] = {
    "OSSIE_SQL_2026": None,
    "ANSI_SQL": None,
    "SNOWFLAKE": "snowflake",
    "DATABRICKS": "databricks",
    "SPARK": "spark",
    "SPARK_SQL": "spark",
    "BIGQUERY": "bigquery",
    "POSTGRES": "postgres",
    "POSTGRESQL": "postgres",
    "DUCKDB": "duckdb",
    "MYSQL": "mysql",
    "REDSHIFT": "redshift",
    "TRINO": "trino",
    "PRESTO": "presto",
    "ATHENA": "athena",
    "TSQL": "tsql",
    "SQLSERVER": "tsql",
    "ORACLE": "oracle",
    "CLICKHOUSE": "clickhouse",
    "SQLITE": "sqlite",
}


@dataclass(frozen=True)
class Expression:
    sql: str | None
    dialects: dict[str, str]  # upper-cased dialect token -> expression text

    @classmethod
    def from_doc(cls, doc: dict[str, Any] | None) -> Expression:
        doc = doc or {}
        dialects: dict[str, str] = {}
        for entry in doc.get("dialects") or []:
            token = str(entry.get("dialect", "")).strip().upper()
            text = entry.get("expression")
            if token and isinstance(text, str) and token not in dialects:
                dialects[token] = text
        sql = doc.get("sql")
        return cls(sql=sql if isinstance(sql, str) else None, dialects=dialects)

    def pick(self, target_sqlglot_dialect: str | None) -> tuple[str, str | None] | None:
        """Choose the expression text for a target engine.

        Returns (text, sqlglot dialect the text is written in), preferring an exact
        engine-specific entry, then the portable forms. Opaque dialects (DAX, MAQL, ...)
        are never used.
        """
        if target_sqlglot_dialect:
            for token, text in self.dialects.items():
                if DIALECT_TO_SQLGLOT.get(token) == target_sqlglot_dialect:
                    return text, target_sqlglot_dialect
        if self.sql:
            return self.sql, None
        for token in PORTABLE_DIALECTS:
            if token in self.dialects:
                return self.dialects[token], None
        for token, text in self.dialects.items():
            if token in DIALECT_TO_SQLGLOT:
                return text, DIALECT_TO_SQLGLOT[token]
        return None

    def display(self) -> str:
        if self.sql:
            return self.sql
        for token in PORTABLE_DIALECTS:
            if token in self.dialects:
                return self.dialects[token]
        return next(iter(self.dialects.values()), "")


def _ai_context(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"instructions": value}
    if isinstance(value, dict):
        return value
    return {}


@dataclass(frozen=True)
class FieldSpec:
    name: str
    dataset: str
    expression: Expression
    datatype: str | None
    is_dimension: bool
    is_time: bool
    label: str | None
    description: str | None
    ai_context: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        return f"{self.dataset}.{self.name}"


@dataclass(frozen=True)
class MetricSpec:
    name: str
    dataset: str | None  # None for model-scoped metrics
    expression: Expression
    datatype: str | None
    description: str | None
    ai_context: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        return f"{self.dataset}.{self.name}" if self.dataset else self.name


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    source: str
    primary_key: tuple[str, ...]
    unique_keys: tuple[tuple[str, ...], ...]
    description: str | None
    ai_context: dict[str, Any]
    fields: dict[str, FieldSpec]
    metrics: dict[str, MetricSpec]


@dataclass(frozen=True)
class RelationshipSpec:
    name: str
    from_dataset: str
    to_dataset: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]
    ai_context: dict[str, Any]


@dataclass(frozen=True)
class ModelSpec:
    name: str
    version: str
    description: str | None
    ai_context: dict[str, Any]
    datasets: dict[str, DatasetSpec]
    relationships: tuple[RelationshipSpec, ...]
    metrics: dict[str, MetricSpec] = field(default_factory=dict)

    def all_metrics(self) -> dict[str, MetricSpec]:
        """Every metric keyed by the name used to query it."""
        out: dict[str, MetricSpec] = {}
        for ds in self.datasets.values():
            for metric in ds.metrics.values():
                out[metric.qualified_name] = metric
        out.update(self.metrics)
        return out

    def all_fields(self) -> dict[str, FieldSpec]:
        return {f.qualified_name: f for ds in self.datasets.values() for f in ds.fields.values()}


def build_model(doc: dict[str, Any]) -> ModelSpec:
    """Build a ModelSpec from a schema-valid document."""
    datasets: dict[str, DatasetSpec] = {}
    for ds_doc in doc.get("datasets") or []:
        ds_name = ds_doc["name"]
        fields: dict[str, FieldSpec] = {}
        for f in ds_doc.get("fields") or []:
            datatype = f.get("datatype")
            dim = f.get("dimension")
            is_time = (dim or {}).get("is_time")
            if is_time is None:
                is_time = datatype in TEMPORAL_TYPES
            fields.setdefault(
                f["name"],
                FieldSpec(
                    name=f["name"],
                    dataset=ds_name,
                    expression=Expression.from_doc(f.get("expression")),
                    datatype=datatype,
                    is_dimension=dim is not None,
                    is_time=bool(is_time),
                    label=f.get("label"),
                    description=f.get("description"),
                    ai_context=_ai_context(f.get("ai_context")),
                ),
            )
        metrics: dict[str, MetricSpec] = {}
        for m in ds_doc.get("metrics") or []:
            metrics.setdefault(
                m["name"],
                MetricSpec(
                    name=m["name"],
                    dataset=ds_name,
                    expression=Expression.from_doc(m.get("expression")),
                    datatype=m.get("datatype"),
                    description=m.get("description"),
                    ai_context=_ai_context(m.get("ai_context")),
                ),
            )
        datasets.setdefault(
            ds_name,
            DatasetSpec(
                name=ds_name,
                source=ds_doc["source"],
                primary_key=tuple(ds_doc.get("primary_key") or ()),
                unique_keys=tuple(tuple(k) for k in ds_doc.get("unique_keys") or ()),
                description=ds_doc.get("description"),
                ai_context=_ai_context(ds_doc.get("ai_context")),
                fields=fields,
                metrics=metrics,
            ),
        )
    relationships = tuple(
        RelationshipSpec(
            name=r["name"],
            from_dataset=r["from"],
            to_dataset=r["to"],
            from_columns=tuple(r.get("from_columns") or ()),
            to_columns=tuple(r.get("to_columns") or ()),
            ai_context=_ai_context(r.get("ai_context")),
        )
        for r in doc.get("relationships") or []
    )
    metrics: dict[str, MetricSpec] = {}
    for m in doc.get("metrics") or []:
        metrics.setdefault(
            m["name"],
            MetricSpec(
                name=m["name"],
                dataset=None,
                expression=Expression.from_doc(m.get("expression")),
                datatype=m.get("datatype"),
                description=m.get("description"),
                ai_context=_ai_context(m.get("ai_context")),
            ),
        )
    return ModelSpec(
        name=doc.get("name", ""),
        version=doc.get("version", ""),
        description=doc.get("description"),
        ai_context=_ai_context(doc.get("ai_context")),
        datasets=datasets,
        relationships=relationships,
        metrics=metrics,
    )
