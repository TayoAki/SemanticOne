"""Helpers for parsing and inspecting SQL expressions declared in a model."""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError


class ExpressionError(ValueError):
    pass


_FORBIDDEN_NODES = (exp.Select, exp.Union, exp.Subquery, exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command)

# Aggregates that give the same answer when rows are duplicated by a join.
_DUP_INSENSITIVE = (exp.ApproxDistinct, exp.Min, exp.Max, exp.AnyValue, exp.LogicalAnd, exp.LogicalOr)


def parse_expr(text: str, dialect: str | None = None) -> exp.Expression:
    """Parse a single scalar/aggregate SQL expression; reject statements and subqueries."""
    if not text or not text.strip():
        raise ExpressionError("Expression is empty")
    try:
        statements = sqlglot.parse(text, read=dialect)
    except (ParseError, TokenError) as err:
        raise ExpressionError(f"Cannot parse expression `{text}`: {_first_line(err)}") from err
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise ExpressionError(f"Expression `{text}` must be a single SQL expression")
    node = statements[0]
    for forbidden in node.find_all(*_FORBIDDEN_NODES):
        raise ExpressionError(
            f"Expression `{text}` contains a {type(forbidden).__name__.lower()}; "
            "only scalar and aggregate expressions are supported"
        )
    return node


def _first_line(err: Exception) -> str:
    return str(err).splitlines()[0] if str(err) else type(err).__name__


def _wrapped(agg: exp.Expression) -> exp.Expression:
    unit = agg
    while isinstance(unit.parent, (exp.Filter, exp.WithinGroup)) and unit.parent.this is unit:
        unit = unit.parent
    return unit


def _is_window_call(unit: exp.Expression) -> bool:
    return isinstance(unit.parent, exp.Window) and unit.parent.this is unit


def _is_group_aggregate(node: exp.Expression) -> bool:
    """True for an aggregate (or its FILTER/WITHIN GROUP wrapper) evaluated per group, not as a window."""
    if isinstance(node, exp.AggFunc):
        return not _is_window_call(_wrapped(node))
    if isinstance(node, (exp.Filter, exp.WithinGroup)):
        return isinstance(node.this, (exp.AggFunc, exp.Filter, exp.WithinGroup)) and not _is_window_call(node)
    return False


def aggregate_units(node: exp.Expression) -> list[exp.Expression]:
    """Outermost per-group aggregate calls, including FILTER / WITHIN GROUP wrappers.

    Window aggregates such as the outer SUM in ``SUM(SUM(x)) OVER (...)`` are not units:
    they are evaluated over the grouped result, and the inner SUM is the unit.
    """
    units: list[exp.Expression] = []
    for agg in node.find_all(exp.AggFunc):
        unit = _wrapped(agg)
        if _is_window_call(unit) or any(unit is u for u in units) or _has_aggregate_ancestor(unit):
            continue
        units.append(unit)
    return units


def _has_aggregate_ancestor(node: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None:
        if _is_group_aggregate(parent):
            return True
        parent = parent.parent
    return False


def contains_aggregate(node: exp.Expression) -> bool:
    return bool(aggregate_units(node))


def contains_window(node: exp.Expression) -> bool:
    return node.find(exp.Window) is not None


def columns(node: exp.Expression) -> list[exp.Column]:
    return [c for c in node.find_all(exp.Column) if not isinstance(c.this, exp.Star)]


def columns_outside_aggregates(node: exp.Expression) -> list[exp.Column]:
    return [c for c in columns(node) if not _has_aggregate_ancestor(c)]


def is_duplicate_insensitive(unit: exp.Expression) -> bool:
    agg = unit
    while isinstance(agg, (exp.Filter, exp.WithinGroup)):
        agg = agg.this
    if isinstance(agg, _DUP_INSENSITIVE):
        return True
    if isinstance(agg, exp.Count) and isinstance(agg.this, exp.Distinct):
        return True
    return False
