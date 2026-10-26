"""Semantic query requests: what a caller asks for, independent of SQL."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

GRAINS = ("hour", "day", "week", "month", "quarter", "year")
FILTER_OPS = (
    "=",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "in",
    "not_in",
    "between",
    "like",
    "not_like",
    "is_null",
    "is_not_null",
)
DEFAULT_LIMIT = 1000


class QueryError(ValueError):
    """The request cannot be answered from this model. Message is safe to show to callers."""


@dataclass(frozen=True)
class DimensionRef:
    field: str  # qualified `dataset.field`
    grain: str | None = None

    @property
    def name(self) -> str:
        return f"{self.field}__{self.grain}" if self.grain else self.field


@dataclass(frozen=True)
class Filter:
    field: str  # `dataset.field` or the name of a requested metric
    op: str
    value: Any = None


@dataclass(frozen=True)
class OrderBy:
    name: str
    desc: bool = False


@dataclass
class QueryRequest:
    metrics: list[str] = field(default_factory=list)
    dimensions: list[DimensionRef] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    order_by: list[OrderBy] = field(default_factory=list)
    limit: int = DEFAULT_LIMIT

    def as_dict(self) -> dict[str, Any]:
        return {
            "metrics": list(self.metrics),
            "dimensions": [d.name for d in self.dimensions],
            "filters": [{"field": f.field, "op": f.op, "value": f.value} for f in self.filters],
            "order_by": [{"name": o.name, "desc": o.desc} for o in self.order_by],
            "limit": self.limit,
        }


def _parse_dimension(raw: Any) -> DimensionRef:
    if isinstance(raw, dict):
        name = raw.get("field") or raw.get("name")
        grain = raw.get("grain")
    else:
        name, grain = raw, None
    if not isinstance(name, str) or not name.strip():
        raise QueryError(f"Invalid dimension {raw!r}; use `dataset.field` or {{\"field\": ..., \"grain\": ...}}")
    name = name.strip()
    if grain is None and "__" in name:
        base, _, suffix = name.rpartition("__")
        if suffix.lower() in GRAINS:
            name, grain = base, suffix
    if grain is not None:
        grain = str(grain).lower()
        if grain not in GRAINS:
            raise QueryError(f"Unknown time grain `{grain}`; use one of {', '.join(GRAINS)}")
    return DimensionRef(field=name, grain=grain)


def parse_query(payload: Any, max_rows: int | None = None) -> QueryRequest:
    """Validate a JSON query payload into a QueryRequest."""
    if not isinstance(payload, dict):
        raise QueryError("Query must be a JSON object")
    metrics = payload.get("metrics") or []
    if isinstance(metrics, str):
        metrics = [metrics]
    if not isinstance(metrics, list) or not all(isinstance(m, str) and m.strip() for m in metrics):
        raise QueryError("`metrics` must be a list of metric names")
    dims_raw = payload.get("dimensions") or []
    if isinstance(dims_raw, str):
        dims_raw = [dims_raw]
    if not isinstance(dims_raw, list):
        raise QueryError("`dimensions` must be a list")
    dimensions = [_parse_dimension(d) for d in dims_raw]
    if not metrics and not dimensions:
        raise QueryError("Ask for at least one metric or dimension")

    filters = []
    for raw in payload.get("filters") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("field"), str):
            raise QueryError("Each filter needs a `field`, an `op` and usually a `value`")
        op = str(raw.get("op", "=")).lower()
        if op not in FILTER_OPS:
            raise QueryError(f"Unknown filter op `{op}`; use one of {', '.join(FILTER_OPS)}")
        value = raw.get("value")
        if op in ("in", "not_in") and (not isinstance(value, list) or not value):
            raise QueryError(f"`{op}` filters need a non-empty list value")
        if op == "between" and (not isinstance(value, list) or len(value) != 2):
            raise QueryError("`between` filters need a two-item list value")
        if op not in ("is_null", "is_not_null", "in", "not_in", "between") and value is None:
            raise QueryError(f"Filter on `{raw['field']}` needs a value")
        _check_values(value)
        filters.append(Filter(field=raw["field"].strip(), op=op, value=value))

    order_by = []
    for raw in payload.get("order_by") or []:
        if isinstance(raw, str):
            desc = raw.startswith("-")
            order_by.append(OrderBy(name=raw.lstrip("-"), desc=desc))
        elif isinstance(raw, dict) and isinstance(raw.get("name") or raw.get("field"), str):
            order_by.append(OrderBy(name=raw.get("name") or raw.get("field"), desc=bool(raw.get("desc"))))
        else:
            raise QueryError("`order_by` entries must be names (prefix `-` for descending) or {name, desc}")

    limit = payload.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise QueryError("`limit` must be a positive integer")
    if max_rows is not None:
        limit = min(limit, max_rows)

    return QueryRequest(
        metrics=[m.strip() for m in metrics],
        dimensions=dimensions,
        filters=filters,
        order_by=order_by,
        limit=limit,
    )


def _check_values(value: Any) -> None:
    values = value if isinstance(value, list) else [value]
    for v in values:
        if v is not None and not isinstance(v, (str, int, float, bool)):
            raise QueryError("Filter values must be strings, numbers, booleans or null")
