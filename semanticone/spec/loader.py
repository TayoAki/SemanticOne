"""Parse, migrate and validate Ossie semantic model documents."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from ..engine import expressions as ex
from .model import DIALECT_TO_SQLGLOT, SPEC_VERSION, ModelSpec, build_model


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning"
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "path": self.path, "message": self.message}


@dataclass
class LoadResult:
    documents: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors and len(self.documents) == 1

    @property
    def document(self) -> dict[str, Any] | None:
        return self.documents[0] if len(self.documents) == 1 else None

    def model(self) -> ModelSpec:
        doc = self.document
        if doc is None or self.errors:
            raise ValueError("Model is not valid")
        return build_model(doc)


class DocumentError(ValueError):
    pass


# --- Parsing ---------------------------------------------------------------


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DocumentError(f"Duplicate key `{key}` (line {key_node.start_mark.line + 1})")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def parse_text(text: str) -> Any:
    """Parse YAML or JSON (JSON is a YAML subset) and reject duplicate keys."""
    if len(text) > 5_000_000:
        raise DocumentError("Document is larger than 5 MB")
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as err:
        raise DocumentError(f"Invalid YAML/JSON: {err}") from err


def dump_yaml(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)


def dump_json(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False)


# --- Migration -------------------------------------------------------------


def migrate(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Convert legacy (0.1.x) wrapped documents into flat 0.2 documents."""
    if not isinstance(raw, dict):
        raise DocumentError("The document root must be a mapping/object")
    notes: list[str] = []
    doc = copy.deepcopy(raw)
    if "semantic_model" in doc:
        wrapped = doc.pop("semantic_model")
        doc.pop("dialects", None)
        doc.pop("vendors", None)
        models = wrapped if isinstance(wrapped, list) else [wrapped]
        models = [m for m in models if isinstance(m, dict)]
        if not models:
            raise DocumentError("`semantic_model` is empty; there is no model to migrate")
        out = []
        for m in models:
            flat = {"version": SPEC_VERSION, **{k: v for k, v in m.items() if k != "version"}}
            out.append(flat)
        notes.append(
            f"Migrated a legacy `semantic_model` wrapper to the flat {SPEC_VERSION} format"
            + (f" ({len(out)} models were split into separate documents)" if len(out) > 1 else "")
        )
        return out, notes
    version = doc.get("version")
    if version != SPEC_VERSION:
        if version is None or str(version).startswith("0.1"):
            notes.append(f"Set `version` to {SPEC_VERSION} (was {version!r})")
            doc = {"version": SPEC_VERSION, **{k: v for k, v in doc.items() if k != "version"}}
    return [doc], notes


# --- Validation ------------------------------------------------------------


@lru_cache
def _schema_validator() -> Draft202012Validator:
    schema_text = resources.files("semanticone.spec").joinpath("schema/ossie-schema.json").read_text()
    return Draft202012Validator(json.loads(schema_text))


def _json_path(parts: Any) -> str:
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out or "(root)"


def schema_issues(doc: dict[str, Any]) -> list[Issue]:
    issues = []
    for err in sorted(_schema_validator().iter_errors(doc), key=lambda e: list(e.absolute_path)):
        message = err.message
        if len(message) > 300:
            message = message[:300] + "…"
        issues.append(Issue("error", _json_path(err.absolute_path), message))
    return issues


def semantic_issues(model: ModelSpec, doc: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []

    def err(path: str, msg: str) -> None:
        issues.append(Issue("error", path, msg))

    def warn(path: str, msg: str) -> None:
        issues.append(Issue("warning", path, msg))

    # Duplicate names (build_model keeps the first occurrence, so detect from the raw doc).
    seen_ds: set[str] = set()
    for ds_doc in doc.get("datasets") or []:
        name = ds_doc.get("name")
        if name in seen_ds:
            err(f"datasets[{name}]", f"Duplicate dataset name `{name}`")
        seen_ds.add(name)
        names: set[str] = set()
        for kind in ("fields", "metrics"):
            for item in ds_doc.get(kind) or []:
                item_name = item.get("name")
                if item_name in names:
                    err(f"datasets[{name}].{kind}[{item_name}]", f"`{item_name}` is declared more than once in dataset `{name}` (fields and metrics share one namespace)")
                names.add(item_name)
    seen: set[str] = set()
    for m in doc.get("metrics") or []:
        if m.get("name") in seen:
            err(f"metrics[{m.get('name')}]", f"Duplicate metric name `{m.get('name')}`")
        seen.add(m.get("name"))
    seen = set()
    for r in doc.get("relationships") or []:
        if r.get("name") in seen:
            err(f"relationships[{r.get('name')}]", f"Duplicate relationship name `{r.get('name')}`")
        seen.add(r.get("name"))

    for ds in model.datasets.values():
        base = f"datasets[{ds.name}]"
        for key_field in ds.primary_key:
            if key_field not in ds.fields:
                err(f"{base}.primary_key", f"Primary key field `{key_field}` is not a declared field of `{ds.name}`")
        for uk in ds.unique_keys:
            for key_field in uk:
                if key_field not in ds.fields:
                    err(f"{base}.unique_keys", f"Unique key field `{key_field}` is not a declared field of `{ds.name}`")
        if not ds.fields:
            warn(base, f"Dataset `{ds.name}` declares no fields, so nothing in it can be queried")
        for f in ds.fields.values():
            fpath = f"{base}.fields[{f.name}]"
            _check_variants(f.expression.dialects, fpath, warn)
            picked = f.expression.pick(None)
            if picked is None:
                warn(fpath, "No SQL expression (OSSIE_SQL_2026, ANSI_SQL or an engine dialect); this field cannot be queried")
                continue
            try:
                node = ex.parse_expr(*picked)
            except ex.ExpressionError as e:
                warn(fpath, f"{e}; this field cannot be queried")
                continue
            if ex.contains_aggregate(node) or ex.contains_window(node):
                err(fpath, "Field expressions must be row-level; move aggregations into a metric")
        for m in ds.metrics.values():
            _check_metric(model, m, f"{base}.metrics[{m.name}]", err, warn)

    for m in model.metrics.values():
        _check_metric(model, m, f"metrics[{m.name}]", err, warn)

    for r in model.relationships:
        rpath = f"relationships[{r.name}]"
        src = model.datasets.get(r.from_dataset)
        dst = model.datasets.get(r.to_dataset)
        if src is None:
            err(rpath, f"`from` dataset `{r.from_dataset}` does not exist")
        if dst is None:
            err(rpath, f"`to` dataset `{r.to_dataset}` does not exist")
        if len(r.from_columns) != len(r.to_columns) or not r.from_columns:
            err(rpath, "`from_columns` and `to_columns` must be non-empty and the same length")
        if src is not None:
            for c in r.from_columns:
                if c not in src.fields:
                    err(rpath, f"`{c}` is not a field of `{src.name}`")
        if dst is not None:
            for c in r.to_columns:
                if c not in dst.fields:
                    err(rpath, f"`{c}` is not a field of `{dst.name}`")
            keys = [set(dst.primary_key)] + [set(k) for k in dst.unique_keys]
            if not any(k and k == set(r.to_columns) for k in keys):
                warn(
                    rpath,
                    f"`to_columns` {list(r.to_columns)} is not a declared primary or unique key of `{dst.name}`; "
                    "if those values are not unique, joins will duplicate rows",
                )
        if r.from_dataset == r.to_dataset:
            warn(rpath, "Self-relationships are not used for joins")
    return issues


def _check_variants(dialects: dict[str, str], path: str, warn) -> None:
    for token, text in dialects.items():
        if token in DIALECT_TO_SQLGLOT:
            try:
                ex.parse_expr(text, DIALECT_TO_SQLGLOT[token])
            except ex.ExpressionError as e:
                warn(path, f"{token} variant: {e}")


def _check_metric(model: ModelSpec, metric, path: str, err, warn) -> None:
    _check_variants(metric.expression.dialects, path, warn)
    picked = metric.expression.pick(None)
    if picked is None:
        warn(path, "No SQL expression (OSSIE_SQL_2026, ANSI_SQL or an engine dialect); this metric cannot be queried")
        return
    try:
        node = ex.parse_expr(*picked)
    except ex.ExpressionError as e:
        warn(path, f"{e}; this metric cannot be queried")
        return
    if not ex.contains_aggregate(node):
        warn(path, "Metric expressions should aggregate (e.g. SUM, COUNT, AVG); this metric cannot be queried")
    outside = ex.columns_outside_aggregates(node)
    if outside:
        needed = ", ".join(sorted({c.sql() for c in outside}))
        warn(path, f"References {needed} outside an aggregate; queries using this metric must group by {needed}")
    for col in ex.columns(node):
        qualifier = col.table
        name = col.name
        if metric.dataset is not None:
            ds = model.datasets[metric.dataset]
            if qualifier and qualifier != metric.dataset:
                err(path, f"`{col.sql()}`: dataset-scoped metrics can only reference fields of `{metric.dataset}`; use a model-scoped metric instead")
            elif name not in ds.fields:
                err(path, f"`{name}` is not a field of `{metric.dataset}`")
        else:
            if not qualifier:
                err(path, f"`{name}` must be qualified as `dataset.field` in a model-scoped metric")
            elif qualifier not in model.datasets:
                err(path, f"`{col.sql()}` references unknown dataset `{qualifier}`")
            elif name not in model.datasets[qualifier].fields:
                err(path, f"`{name}` is not a field of `{qualifier}`")


def load(text: str) -> LoadResult:
    """Parse, migrate and fully validate a document. Never raises on bad input."""
    result = LoadResult()
    try:
        raw = parse_text(text)
        docs, notes = migrate(raw)
    except DocumentError as e:
        result.issues.append(Issue("error", "(root)", str(e)))
        return result
    result.documents = docs
    result.notes = notes
    if len(docs) > 1:
        result.issues.append(
            Issue("error", "(root)", f"The document contains {len(docs)} models; import them as separate models")
        )
        return result
    doc = docs[0]
    result.issues.extend(schema_issues(doc))
    if result.errors:
        return result
    model = build_model(doc)
    result.issues.extend(semantic_issues(model, doc))
    return result
