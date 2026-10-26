"""Compile semantic queries against an Ossie model into SQL.

Correctness model
-----------------
Relationships run from the many side (`from`) to the one side (`to`), so joining
along them never duplicates rows of the starting ("root") dataset. Each aggregate
inside a metric is evaluated in its own grouped subquery rooted at a dataset it
aggregates, joining only along many-to-one paths. Duplicate-insensitive aggregates
(COUNT DISTINCT, MIN, MAX, ...) may instead be rooted at any dataset that reaches
everything they need. Subqueries are then combined on the requested dimensions, so
metrics from different fact tables never double count (no fan-out / chasm traps).
If no safe join path exists, compilation fails with an explanation instead of
returning a wrong number.
"""

from __future__ import annotations

import difflib
import re
from collections import deque
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from ..spec.model import DatasetSpec, FieldSpec, MetricSpec, ModelSpec, RelationshipSpec
from . import expressions as ex
from .query import DimensionRef, Filter, QueryError, QueryRequest

_PREFIX = "so_"


@dataclass
class CompiledQuery:
    sql: str
    columns: list[str]
    warnings: list[str] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)


@dataclass
class _Unit:
    node: exp.Expression
    datasets: frozenset[str]
    dup_insensitive: bool
    sql_key: str
    root: str = ""
    column: str = ""


@dataclass
class _MetricPlan:
    name: str
    template: exp.Expression
    units: list[_Unit]
    outside: dict[str, FieldSpec]  # placeholder column name -> field it stands for


def _safe(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return cleaned


def _ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def _col(name: str, table: str | None = None) -> exp.Column:
    return exp.Column(this=_ident(name), table=_ident(table) if table else None)


class JoinGraph:
    """Directed many-to-one graph over datasets."""

    def __init__(self, model: ModelSpec):
        self.model = model
        self.edges: dict[str, list[RelationshipSpec]] = {name: [] for name in model.datasets}
        for r in model.relationships:
            if (
                r.from_dataset != r.to_dataset
                and r.from_dataset in model.datasets
                and r.to_dataset in model.datasets
                and r.from_columns
                and len(r.from_columns) == len(r.to_columns)
            ):
                self.edges[r.from_dataset].append(r)
        self._trees: dict[str, tuple[dict[str, RelationshipSpec | None], dict[str, int], set[str]]] = {}

    def tree(self, root: str) -> tuple[dict[str, RelationshipSpec | None], dict[str, int], set[str]]:
        """BFS tree from root: parent edge per dataset, depth, and datasets reachable two ways."""
        if root not in self._trees:
            parent: dict[str, RelationshipSpec | None] = {root: None}
            depth = {root: 0}
            ambiguous: set[str] = set()
            queue = deque([root])
            while queue:
                node = queue.popleft()
                for edge in self.edges.get(node, []):
                    target = edge.to_dataset
                    if target not in parent:
                        parent[target] = edge
                        depth[target] = depth[node] + 1
                        queue.append(target)
                    elif target != root and parent[target] is not edge:
                        ambiguous.add(target)
            self._trees[root] = (parent, depth, ambiguous)
        return self._trees[root]

    def reachable(self, root: str) -> set[str]:
        return set(self.tree(root)[0])

    def path_edges(self, root: str, targets: set[str]) -> list[RelationshipSpec]:
        parent, depth, _ = self.tree(root)
        needed: dict[str, RelationshipSpec] = {}
        for target in targets:
            node = target
            while parent.get(node) is not None:
                edge = parent[node]
                assert edge is not None
                needed[node] = edge
                node = edge.from_dataset
        return [needed[name] for name in sorted(needed, key=lambda n: (depth[n], n))]


class Compiler:
    def __init__(self, model: ModelSpec, dialect: str | None):
        self.model = model
        self.dialect = dialect
        self.graph = JoinGraph(model)
        self._fields = model.all_fields()
        self._metrics = model.all_metrics()
        self._field_cache: dict[str, exp.Expression] = {}
        self._dataset_order = {name: i for i, name in enumerate(model.datasets)}
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ names

    def alias_of(self, dataset: str) -> str:
        return _safe(dataset)

    def _unknown(self, kind: str, name: str, options: list[str]) -> QueryError:
        close = difflib.get_close_matches(name, options, n=3, cutoff=0.5)
        hint = f" Did you mean {', '.join(f'`{c}`' for c in close)}?" if close else ""
        return QueryError(f"Unknown {kind} `{name}`.{hint}")

    def field(self, qualified: str) -> FieldSpec:
        f = self._fields.get(qualified)
        if f is None:
            raise self._unknown("field", qualified, list(self._fields))
        return f

    # ------------------------------------------------------------ expressions

    def field_expr(self, f: FieldSpec) -> exp.Expression:
        key = f.qualified_name
        if key not in self._field_cache:
            picked = f.expression.pick(self.dialect)
            if picked is None:
                raise QueryError(f"Field `{key}` has no SQL expression this engine can run")
            try:
                node = ex.parse_expr(*picked)
            except ex.ExpressionError as e:
                raise QueryError(f"Field `{key}`: {e}") from e
            if ex.contains_aggregate(node) or ex.contains_window(node):
                raise QueryError(f"Field `{key}` must be a row-level expression")
            alias = self.alias_of(f.dataset)

            def qualify(n: exp.Expression) -> exp.Expression:
                if isinstance(n, exp.Column) and not isinstance(n.this, exp.Star):
                    return exp.Column(this=n.this.copy(), table=_ident(alias))
                return n

            self._field_cache[key] = node.transform(qualify)
        return self._field_cache[key].copy()

    def dimension_expr(self, dim: DimensionRef) -> exp.Expression:
        f = self.field(dim.field)
        node = self.field_expr(f)
        if dim.grain:
            unit = exp.Var(this=dim.grain.upper())
            if f.datatype == "Date" and dim.grain != "hour":
                node = exp.DateTrunc(this=node, unit=unit)
            else:
                node = exp.TimestampTrunc(this=node, unit=unit)
        return node

    def _literal(self, value, datatype: str | None) -> exp.Expression:
        if value is None:
            return exp.Null()
        if isinstance(value, bool):
            return exp.Boolean(this=value)
        if isinstance(value, (int, float)):
            return exp.Literal.number(value)
        lit = exp.Literal.string(str(value))
        if datatype == "Date":
            return exp.cast(lit, "DATE")
        if datatype in ("DateTime", "DateTimeTz"):
            return exp.cast(lit, "TIMESTAMP")
        return lit

    def _predicate(self, target: exp.Expression, flt: Filter, datatype: str | None) -> exp.Expression:
        op, value = flt.op, flt.value
        lit = lambda v: self._literal(v, datatype)  # noqa: E731
        if op == "is_null":
            return exp.Is(this=target, expression=exp.Null())
        if op == "is_not_null":
            return exp.Not(this=exp.Is(this=target, expression=exp.Null()))
        if op in ("in", "not_in"):
            node = exp.In(this=target, expressions=[lit(v) for v in value])
            return exp.Not(this=node) if op == "not_in" else node
        if op == "between":
            return exp.Between(this=target, low=lit(value[0]), high=lit(value[1]))
        if op in ("like", "not_like"):
            node = exp.Like(this=target, expression=exp.Literal.string(str(value)))
            return exp.Not(this=node) if op == "not_like" else node
        cls = {"=": exp.EQ, "!=": exp.NEQ, ">": exp.GT, ">=": exp.GTE, "<": exp.LT, "<=": exp.LTE}[op]
        return cls(this=target, expression=lit(value))

    def _table(self, ds: DatasetSpec) -> exp.Expression:
        alias = exp.TableAlias(this=_ident(self.alias_of(ds.name)))
        source = ds.source.strip()
        if re.match(r"(?is)^\s*(select|with)\b", source):
            try:
                query = sqlglot.parse_one(source, read=self.dialect)
            except sqlglot.errors.ParseError as e:
                raise QueryError(f"Dataset `{ds.name}` source query cannot be parsed: {e}") from e
            if not isinstance(query, exp.Query):
                raise QueryError(f"Dataset `{ds.name}` source must be a table name or a SELECT query")
            return exp.Subquery(this=query, alias=alias)
        try:
            table = exp.to_table(source, dialect=self.dialect)
        except Exception as e:  # sqlglot raises several error types for bad names
            raise QueryError(f"Dataset `{ds.name}` has an invalid source `{source}`") from e
        table.set("alias", alias)
        return table

    # ---------------------------------------------------------------- metrics

    def _analyze_metric(self, name: str) -> _MetricPlan:
        metric = self._metrics.get(name)
        if metric is None:
            raise self._unknown("metric", name, list(self._metrics))
        picked = metric.expression.pick(self.dialect)
        if picked is None:
            raise QueryError(f"Metric `{name}` has no SQL expression this engine can run")
        try:
            node = ex.parse_expr(*picked)
        except ex.ExpressionError as e:
            raise QueryError(f"Metric `{name}`: {e}") from e
        units = ex.aggregate_units(node)
        if not units:
            raise QueryError(f"Metric `{name}` does not aggregate, so it cannot be queried")

        def resolve(col: exp.Column) -> FieldSpec:
            if metric.dataset is not None:
                if col.table and col.table != metric.dataset:
                    raise QueryError(f"Metric `{name}` may only reference fields of `{metric.dataset}`")
                ds_name = metric.dataset
            else:
                if not col.table:
                    raise QueryError(f"Metric `{name}`: `{col.name}` must be qualified as `dataset.field`")
                ds_name = col.table
            ds = self.model.datasets.get(ds_name)
            if ds is None or col.name not in ds.fields:
                raise QueryError(f"Metric `{name}` references unknown field `{ds_name}.{col.name}`")
            return ds.fields[col.name]

        plan_units: list[_Unit] = []
        for i, unit in enumerate(units):
            referenced = [resolve(c) for c in ex.columns(unit)]
            datasets = frozenset(f.dataset for f in referenced)
            if not datasets:
                if metric.dataset is None:
                    raise QueryError(
                        f"Metric `{name}`: `{unit.sql()}` does not say which dataset to count; "
                        "use e.g. COUNT(dataset.key) in model-scoped metrics"
                    )
                datasets = frozenset({metric.dataset})
            substituted = unit.copy().transform(
                lambda n: exp.Paren(this=self.field_expr(resolve(n)))
                if isinstance(n, exp.Column) and not isinstance(n.this, exp.Star)
                else n
            )
            plan_units.append(
                _Unit(
                    node=substituted,
                    datasets=datasets,
                    dup_insensitive=ex.is_duplicate_insensitive(unit),
                    sql_key=substituted.sql(),
                )
            )
            placeholder_col = exp.column(f"__u{i}")
            if unit is node:
                node = placeholder_col
            else:
                unit.replace(placeholder_col)

        outside: dict[str, FieldSpec] = {}
        for j, col in enumerate(ex.columns(node)):
            if col.name.startswith("__u") and not col.table:
                continue
            placeholder = f"__o{j}"
            outside[placeholder] = resolve(col)
            col.replace(exp.column(placeholder))
        return _MetricPlan(name=name, template=node, units=plan_units, outside=outside)

    def _choose_root(self, unit: _Unit, required: set[str], metric: str, needed_by: dict[str, set[str]]) -> str:
        needed = set(unit.datasets) | required
        candidates = []
        for root in self.model.datasets:
            if not needed <= self.graph.reachable(root):
                continue
            if not unit.dup_insensitive and root not in unit.datasets:
                continue
            cost = len(self.graph.path_edges(root, needed))
            candidates.append((root not in unit.datasets, cost, self._dataset_order[root], root))
        if candidates:
            return min(candidates)[-1]
        # Explain why no safe plan exists.
        sources = ", ".join(f"`{d}`" for d in sorted(unit.datasets))
        if len(unit.datasets) > 1 and not any(
            set(unit.datasets) <= self.graph.reachable(d) for d in unit.datasets
        ):
            raise QueryError(
                f"Metric `{metric}`: `{unit.node.sql()}` mixes {sources}, which are not linked by "
                "many-to-one relationships, so it cannot be computed without double counting"
            )
        best = max(unit.datasets, key=lambda d: len(needed & self.graph.reachable(d)))
        missing = sorted(needed - self.graph.reachable(best))
        fields = sorted({name for m in missing for name in needed_by.get(m, ())})
        because = f" (needed by {', '.join(f'`{f}`' for f in fields)})" if fields else ""
        raise QueryError(
            f"Metric `{metric}` aggregates {sources}, which has no many-to-one join path to "
            f"{', '.join(f'`{m}`' for m in missing)}{because}. Grouping or filtering this way "
            "would double count, so the query was refused. Add a relationship or choose other dimensions."
        )

    # ---------------------------------------------------------------- compile

    def compile(self, q: QueryRequest) -> CompiledQuery:
        if len(set(q.metrics)) != len(q.metrics):
            raise QueryError("A metric is requested more than once")
        dims = q.dimensions
        dim_names = [d.name for d in dims]
        if len(set(dim_names)) != len(dim_names):
            raise QueryError("A dimension is requested more than once")
        dim_specs = [self.field(d.field) for d in dims]
        dim_aliases = [f"d{i}_{_safe(d.name)}"[:60] for i, d in enumerate(dims)]
        metric_aliases = {m: f"m{i}_{_safe(m)}"[:60] for i, m in enumerate(q.metrics)}

        metric_filters: list[Filter] = []
        dim_filters: list[tuple[Filter, FieldSpec]] = []
        for flt in q.filters:
            if flt.field in metric_aliases:
                metric_filters.append(flt)
            elif flt.field in self._metrics:
                raise QueryError(f"To filter on metric `{flt.field}`, include it in `metrics`")
            else:
                dim_filters.append((flt, self.field(flt.field)))

        required = {f.dataset for f in dim_specs} | {f.dataset for _, f in dim_filters}
        needed_by: dict[str, set[str]] = {}
        for d, f in zip(dims, dim_specs):
            needed_by.setdefault(f.dataset, set()).add(d.field)
        for flt, f in dim_filters:
            needed_by.setdefault(f.dataset, set()).add(f"filter on {flt.field}")

        plans = [self._analyze_metric(m) for m in q.metrics]

        # Assign each aggregate a root and a column; dedupe identical aggregates per root.
        roots: dict[str, list[_Unit]] = {}
        for plan in plans:
            for unit in plan.units:
                unit.root = self._choose_root(unit, required, plan.name, needed_by)
                bucket = roots.setdefault(unit.root, [])
                existing = next((u for u in bucket if u.sql_key == unit.sql_key), None)
                if existing is not None:
                    unit.column = existing.column
                else:
                    unit.column = f"a{sum(len(b) for b in roots.values())}"
                    bucket.append(unit)

        if not plans:
            # Dimension-only query: pick the dataset that reaches every dimension most cheaply.
            options = [
                (len(self.graph.path_edges(r, required)), r not in required, self._dataset_order[r], r)
                for r in self.model.datasets
                if required <= self.graph.reachable(r)
            ]
            if not options:
                raise QueryError(
                    "These dimensions are not connected by many-to-one relationships, so they cannot be combined"
                )
            roots[min(options)[-1]] = []

        # Build one grouped CTE per root.
        ctes: list[tuple[str, exp.Select]] = []
        cte_of_root: dict[str, str] = {}
        plan_info: list[dict] = []
        for i, (root, units) in enumerate(roots.items()):
            cte_name = f"{_PREFIX}agg_{i}_{_safe(root)}"[:60]
            cte_of_root[root] = cte_name
            needed = set(required)
            for u in units:
                needed |= set(u.datasets)
            edges = self.graph.path_edges(root, needed)
            _, _, ambiguous = self.graph.tree(root)
            for name in sorted(ambiguous & needed):
                self.warnings.append(
                    f"`{name}` can be reached from `{root}` through more than one relationship; "
                    f"using `{next(e.name for e in edges if e.to_dataset == name)}`"
                )
            select = exp.Select()
            for alias, dim in zip(dim_aliases, dims):
                select = select.select(exp.alias_(self.dimension_expr(dim), alias, quoted=True), copy=False)
            for u in units:
                select = select.select(exp.alias_(u.node.copy(), u.column, quoted=True), copy=False)
            select = select.from_(self._table(self.model.datasets[root]), copy=False)
            for edge in edges:
                left = self.model.datasets[edge.from_dataset]
                right = self.model.datasets[edge.to_dataset]
                conds = [
                    exp.EQ(this=self.field_expr(left.fields[a]), expression=self.field_expr(right.fields[b]))
                    for a, b in zip(edge.from_columns, edge.to_columns)
                ]
                select = select.join(self._table(right), on=exp.and_(*conds), join_type="LEFT", copy=False)
            for flt, f in dim_filters:
                select = select.where(self._predicate(self.field_expr(f), flt, f.datatype), copy=False)
            if dims:
                select = select.group_by(*[self.dimension_expr(d) for d in dims], copy=False)
            ctes.append((cte_name, select))
            plan_info.append(
                {
                    "cte": cte_name,
                    "root": root,
                    "joins": [f"{e.from_dataset} -> {e.to_dataset} ({e.name})" for e in edges],
                    "aggregates": [u.node.sql(dialect=self.dialect) for u in units],
                }
            )

        # Combine CTEs on the dimensions.
        cte_names = [name for name, _ in ctes]
        if len(ctes) == 1:
            base = cte_names[0]
            from_expr: exp.Expression = exp.to_table(base)
            joins: list[tuple[exp.Expression, exp.Expression | None]] = []
        elif dims:
            spine = None
            for name in cte_names:
                part = exp.select(*[_col(a) for a in dim_aliases]).from_(name)
                spine = part if spine is None else exp.union(spine, part, distinct=True)
            base = f"{_PREFIX}spine"
            ctes.append((base, spine))
            from_expr = exp.to_table(base)
            joins = [
                (
                    exp.to_table(name),
                    exp.and_(*[exp.NullSafeEQ(this=_col(a, base), expression=_col(a, name)) for a in dim_aliases]),
                )
                for name in cte_names
            ]
        else:
            base = cte_names[0]
            from_expr = exp.to_table(base)
            joins = [(exp.to_table(name), None) for name in cte_names[1:]]

        dim_ref = {d.field: _col(alias, base) for d, alias in zip(dims, dim_aliases)}
        final = exp.Select()
        for alias in dim_aliases:
            final = final.select(exp.alias_(_col(alias, base), alias, quoted=True), copy=False)
        for plan in plans:
            replacements: dict[str, exp.Expression] = {}
            for i, unit in enumerate(plan.units):
                replacements[f"__u{i}"] = _col(unit.column, cte_of_root[unit.root])
            for placeholder, f in plan.outside.items():
                ref = dim_ref.get(f.qualified_name)
                if ref is None:
                    raise QueryError(f"Metric `{plan.name}` requires grouping by `{f.qualified_name}`")
                replacements[placeholder] = ref
            expr = plan.template.copy().transform(
                lambda n: replacements[n.name].copy()
                if isinstance(n, exp.Column) and not n.table and n.name in replacements
                else n
            )
            final = final.select(exp.alias_(expr, metric_aliases[plan.name], quoted=True), copy=False)
        final = final.from_(from_expr, copy=False)
        for table, on in joins:
            if on is None:
                final = final.join(table, join_type="CROSS", copy=False)
            else:
                final = final.join(table, on=on, join_type="LEFT", copy=False)
        final_name = f"{_PREFIX}result"
        ctes.append((final_name, final))

        out_aliases = dim_aliases + [metric_aliases[m] for m in q.metrics]
        outer = exp.select(*[_col(a) for a in out_aliases]).from_(final_name)
        for name, select in ctes:
            outer = outer.with_(name, as_=select, copy=False)
        for flt in metric_filters:
            outer = outer.where(self._predicate(_col(metric_aliases[flt.field]), flt, None), copy=False)

        by_name = dict(zip(dim_names, dim_aliases)) | metric_aliases
        if q.order_by:
            for o in q.order_by:
                if o.name not in by_name:
                    raise QueryError(f"Cannot order by `{o.name}`: it is not a requested dimension or metric")
                outer = outer.order_by(exp.Ordered(this=_col(by_name[o.name]), desc=o.desc), copy=False)
        elif dims:
            outer = outer.order_by(*[_col(a) for a in dim_aliases], copy=False)
        outer = outer.limit(q.limit, copy=False)

        try:
            sql = outer.sql(dialect=self.dialect, pretty=True)
        except sqlglot.errors.UnsupportedError as e:  # pragma: no cover - dialect gaps
            raise QueryError(f"This query cannot be expressed in the target SQL dialect: {e}") from e
        return CompiledQuery(sql=sql, columns=dim_names + list(q.metrics), warnings=self.warnings, plan=plan_info)


def compile_query(model: ModelSpec, q: QueryRequest, dialect: str | None) -> CompiledQuery:
    return Compiler(model, dialect).compile(q)


def metric_datasets(model: ModelSpec, metric: MetricSpec) -> list[str]:
    """Datasets a metric aggregates, in model order (empty if its expression is unusable)."""
    if metric.dataset is not None:
        return [metric.dataset]
    picked = metric.expression.pick(None)
    if picked is None:
        return []
    try:
        node = ex.parse_expr(*picked)
    except ex.ExpressionError:
        return []
    names = {c.table for c in ex.columns(node) if c.table in model.datasets}
    return [name for name in model.datasets if name in names]


def default_time_dimension(model: ModelSpec, metric: MetricSpec, graph: JoinGraph | None = None) -> str | None:
    """The nearest time field reachable from the metric's datasets: its natural time axis."""
    graph = graph or JoinGraph(model)
    best: tuple[int, int, int, str] | None = None
    order = {name: i for i, name in enumerate(model.datasets)}
    for root in metric_datasets(model, metric):
        _, depth, _ = graph.tree(root)
        for ds_name, distance in depth.items():
            for i, f in enumerate(model.datasets[ds_name].fields.values()):
                if f.is_time:
                    candidate = (distance, 0 if f.is_dimension else 1, order[ds_name] * 1000 + i, f.qualified_name)
                    if best is None or candidate < best:
                        best = candidate
    return best[-1] if best else None
