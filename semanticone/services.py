"""Application services shared by the web UI, REST API and MCP server."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import security
from .engine.compiler import CompiledQuery, JoinGraph, compile_query, default_time_dimension
from .engine.connectors import (
    KINDS,
    ConnectorError,
    DemoConnector,
    SQLAlchemyConnector,
    dialect_for,
    dispose_engine,
    validate_connection_url,
)
from .engine.demo import DEMO_MODEL_YAML
from .engine.query import QueryError, parse_query
from .models import (
    ROLE_OWNER,
    ApiKey,
    Connection,
    Membership,
    ModelVersion,
    Organization,
    QueryLog,
    SemanticModel,
    User,
    utcnow,
)
from .plans import UNLIMITED, Plan, get_plan
from .spec import loader
from .spec.model import ModelSpec


class ServiceError(ValueError):
    """A user-facing error (bad input, limits, permissions)."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class LimitError(ServiceError):
    def __init__(self, message: str):
        super().__init__(message, status_code=402)


# --- helpers ---------------------------------------------------------------


def slugify(value: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:60] or fallback


def _unique_slug(db: Session, base: str, exists) -> str:
    slug = base
    n = 2
    while exists(slug):
        slug = f"{base}-{n}"[:70]
        n += 1
    return slug


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def plan_for(org: Organization) -> Plan:
    plan = get_plan(org.plan)
    # A paid plan whose subscription is no longer active falls back to free limits.
    if org.plan in ("pro", "business") and org.subscription_status not in (None, "active", "trialing", "past_due"):
        return get_plan("free")
    return plan


def _check_limit(limit: int, current: int, message: str) -> None:
    if limit != UNLIMITED and current >= limit:
        raise LimitError(message)


# --- users & organizations -------------------------------------------------


def create_user(db: Session, email: str, password: str, name: str = "") -> User:
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ServiceError("Enter a valid email address.")
    problem = security.password_problem(password)
    if problem:
        raise ServiceError(problem)
    if db.scalar(select(User).where(User.email == email)):
        raise ServiceError("An account with this email already exists.")
    user = User(email=email, name=name.strip()[:200], password_hash=security.hash_password(password))
    db.add(user)
    db.flush()
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not user.is_active:
        security.hash_password(password)  # equalize timing
        return None
    return user if security.verify_password(password, user.password_hash) else None


def create_organization(db: Session, owner: User, name: str, with_sample: bool = True) -> Organization:
    name = name.strip() or "My workspace"
    slug = _unique_slug(
        db, slugify(name, "workspace"), lambda s: db.scalar(select(Organization.id).where(Organization.slug == s))
    )
    org = Organization(name=name[:200], slug=slug)
    db.add(org)
    db.flush()
    db.add(Membership(user_id=owner.id, organization_id=org.id, role=ROLE_OWNER))
    if with_sample:
        demo = Connection(organization_id=org.id, name="Sample warehouse", kind="demo", encrypted_url="")
        db.add(demo)
        db.flush()
        create_model(db, org, owner, DEMO_MODEL_YAML, connection_id=demo.id, message="Sample model", enforce_limits=False)
    db.flush()
    return org


def memberships_for(db: Session, user: User) -> list[Membership]:
    return list(
        db.scalars(select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at))
    )


# --- semantic models -------------------------------------------------------


@dataclass
class SaveResult:
    model: SemanticModel
    version: ModelVersion | None
    load: loader.LoadResult
    changed: bool


def normalize_content(text: str) -> str:
    text = text.replace("\r\n", "\n")
    return text if text.endswith("\n") else text + "\n"


def create_model(
    db: Session,
    org: Organization,
    user: User | None,
    content: str,
    *,
    connection_id: int | None = None,
    message: str = "Initial version",
    enforce_limits: bool = True,
) -> SaveResult:
    if enforce_limits:
        count = db.scalar(select(func.count(SemanticModel.id)).where(SemanticModel.organization_id == org.id)) or 0
        plan = plan_for(org)
        _check_limit(plan.max_models, count, f"Your {plan.name} plan allows {plan.max_models} models. Upgrade to add more.")
    content = normalize_content(content)
    result = loader.load(content)
    if result.errors:
        raise ValidationFailed(result)
    doc = result.document
    assert doc is not None
    if result.notes:
        content = loader.dump_yaml(doc)
    name = str(doc.get("name") or "Untitled model")
    slug = _unique_slug(
        db,
        slugify(name, "model"),
        lambda s: db.scalar(
            select(SemanticModel.id).where(SemanticModel.organization_id == org.id, SemanticModel.slug == s)
        ),
    )
    if connection_id is not None:
        conn = db.get(Connection, connection_id)
        if conn is None or conn.organization_id != org.id:
            raise ServiceError("Unknown connection")
    model = SemanticModel(
        organization_id=org.id,
        slug=slug,
        name=name[:200],
        description=str(doc.get("description") or "")[:2000],
        connection_id=connection_id,
        current_version=0,
    )
    db.add(model)
    db.flush()
    version = _add_version(db, model, content, user, message)
    return SaveResult(model=model, version=version, load=result, changed=True)


class ValidationFailed(ServiceError):
    def __init__(self, result: loader.LoadResult):
        first = result.errors[0] if result.errors else None
        summary = f"{len(result.errors)} validation error(s)"
        if first:
            summary += f"; first: {first.path}: {first.message}"
        super().__init__(summary, status_code=422)
        self.result = result


def _add_version(db: Session, model: SemanticModel, content: str, user: User | None, message: str) -> ModelVersion:
    model.current_version += 1
    version = ModelVersion(
        semantic_model_id=model.id,
        version=model.current_version,
        content=content,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
        message=message[:500],
        created_by_id=user.id if user else None,
    )
    db.add(version)
    model.updated_at = utcnow()
    db.flush()
    return version


def save_model(db: Session, model: SemanticModel, user: User | None, content: str, message: str = "") -> SaveResult:
    content = normalize_content(content)
    result = loader.load(content)
    if result.errors:
        raise ValidationFailed(result)
    doc = result.document
    assert doc is not None
    if result.notes:
        content = loader.dump_yaml(doc)
    current = current_version(db, model)
    if current is not None and current.checksum == hashlib.sha256(content.encode()).hexdigest():
        return SaveResult(model=model, version=current, load=result, changed=False)
    model.name = str(doc.get("name") or model.name)[:200]
    model.description = str(doc.get("description") or "")[:2000]
    version = _add_version(db, model, content, user, message or f"Version {model.current_version + 1}")
    return SaveResult(model=model, version=version, load=result, changed=True)


def current_version(db: Session, model: SemanticModel) -> ModelVersion | None:
    return db.scalar(
        select(ModelVersion).where(
            ModelVersion.semantic_model_id == model.id, ModelVersion.version == model.current_version
        )
    )


def get_version(db: Session, model: SemanticModel, number: int) -> ModelVersion | None:
    return db.scalar(
        select(ModelVersion).where(ModelVersion.semantic_model_id == model.id, ModelVersion.version == number)
    )


def model_spec(db: Session, model: SemanticModel) -> ModelSpec:
    version = current_version(db, model)
    if version is None:
        raise ServiceError("This model has no versions yet", 404)
    return _spec_from_content(version.checksum, version.content)


_SPEC_CACHE: dict[str, ModelSpec] = {}


def _spec_from_content(checksum: str, content: str) -> ModelSpec:
    spec = _SPEC_CACHE.get(checksum)
    if spec is None:
        result = loader.load(content)
        if result.errors:
            raise ServiceError("The stored model is invalid; open the editor to fix it", 409)
        spec = result.model()
        if len(_SPEC_CACHE) > 512:
            _SPEC_CACHE.clear()
        _SPEC_CACHE[checksum] = spec
    return spec


def find_model(db: Session, org: Organization, slug: str) -> SemanticModel:
    model = db.scalar(
        select(SemanticModel).where(SemanticModel.organization_id == org.id, SemanticModel.slug == slug)
    )
    if model is None:
        raise ServiceError(f"Model `{slug}` not found", 404)
    return model


def list_models(db: Session, org: Organization) -> list[SemanticModel]:
    return list(
        db.scalars(
            select(SemanticModel).where(SemanticModel.organization_id == org.id).order_by(SemanticModel.name)
        )
    )


def catalog(spec: ModelSpec) -> dict[str, Any]:
    """A compact, AI-friendly description of what can be queried."""

    def ai(ctx: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in ctx.items() if k in ("instructions", "synonyms", "examples") and v}

    dimensions = []
    for f in spec.all_fields().values():
        entry: dict[str, Any] = {"name": f.qualified_name, "datatype": f.datatype}
        if f.description:
            entry["description"] = f.description
        if f.is_time:
            entry["time"] = True
            entry["grains"] = ["day", "week", "month", "quarter", "year"]
        if not f.is_dimension:
            entry["role"] = "attribute"
        entry.update(ai(f.ai_context))
        dimensions.append(entry)
    metrics = []
    graph = JoinGraph(spec)
    # Model-level metrics are the curated business metrics, so list them first.
    ordered = sorted(spec.all_metrics().items(), key=lambda item: item[1].dataset is not None)
    for name, m in ordered:
        entry = {"name": name, "expression": m.expression.display()}
        time_dim = default_time_dimension(spec, m, graph)
        if time_dim:
            entry["default_time_dimension"] = time_dim
        if m.description:
            entry["description"] = m.description
        if m.datatype:
            entry["datatype"] = m.datatype
        entry.update(ai(m.ai_context))
        metrics.append(entry)
    return {
        "name": spec.name,
        "description": spec.description,
        **({"ai_context": ai(spec.ai_context)} if ai(spec.ai_context) else {}),
        "datasets": [
            {
                "name": ds.name,
                "description": ds.description,
                "primary_key": list(ds.primary_key),
                **ai(ds.ai_context),
            }
            for ds in spec.datasets.values()
        ],
        "relationships": [
            {"name": r.name, "from": r.from_dataset, "to": r.to_dataset, "on": list(zip(r.from_columns, r.to_columns))}
            for r in spec.relationships
        ],
        "metrics": metrics,
        "dimensions": dimensions,
    }


# --- connections -----------------------------------------------------------


def create_connection(db: Session, org: Organization, name: str, kind: str, url: str) -> Connection:
    plan = plan_for(org)
    name = name.strip()
    if not name:
        raise ServiceError("Give the connection a name.")
    if kind not in KINDS:
        raise ServiceError("Unsupported connection type.")
    if kind != "demo" and not plan.warehouse_connections:
        raise LimitError(f"Warehouse connections are available on paid plans. Upgrade from {plan.name} to connect your data.")
    count = db.scalar(select(func.count(Connection.id)).where(Connection.organization_id == org.id, Connection.kind != "demo")) or 0
    if kind != "demo":
        _check_limit(plan.max_connections, count, f"Your {plan.name} plan allows {plan.max_connections} warehouse connections.")
    if db.scalar(select(Connection.id).where(Connection.organization_id == org.id, Connection.name == name)):
        raise ServiceError("A connection with this name already exists.")
    encrypted = ""
    if kind != "demo":
        try:
            normalized = validate_connection_url(kind, url)
        except ConnectorError as e:
            raise ServiceError(str(e)) from e
        encrypted = security.encrypt(normalized)
    conn = Connection(organization_id=org.id, name=name[:120], kind=kind, encrypted_url=encrypted)
    db.add(conn)
    db.flush()
    return conn


def delete_connection(db: Session, conn: Connection) -> None:
    dispose_engine((conn.id, conn.updated_at.isoformat() if conn.updated_at else ""))
    db.delete(conn)
    db.flush()


def connector_for(conn: Connection):
    if conn.kind == "demo":
        return DemoConnector()
    url = security.decrypt(conn.encrypted_url)
    # Re-check the host at use time so DNS changes cannot point a connection at internal services.
    validate_connection_url(conn.kind, url)
    return SQLAlchemyConnector(conn.kind, url, cache_key=(conn.id, conn.updated_at.isoformat() if conn.updated_at else ""))


def redacted_url(conn: Connection) -> str:
    if conn.kind == "demo" or not conn.encrypted_url:
        return ""
    try:
        url = security.decrypt(conn.encrypted_url)
    except ValueError:
        return "(cannot decrypt)"
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1•••••@", url)


def test_connection(conn: Connection) -> str:
    try:
        return connector_for(conn).test()
    except ConnectorError as e:
        raise ServiceError(f"Connection failed: {e}") from e


# --- API keys --------------------------------------------------------------


def create_api_key(db: Session, org: Organization, user: User | None, name: str) -> tuple[ApiKey, str]:
    plan = plan_for(org)
    count = db.scalar(
        select(func.count(ApiKey.id)).where(ApiKey.organization_id == org.id, ApiKey.revoked_at.is_(None))
    ) or 0
    _check_limit(plan.max_api_keys, count, f"Your {plan.name} plan allows {plan.max_api_keys} active API keys.")
    plaintext, prefix, key_hash = security.generate_api_key()
    key = ApiKey(
        organization_id=org.id,
        name=(name.strip() or "API key")[:120],
        prefix=prefix,
        key_hash=key_hash,
        created_by_id=user.id if user else None,
    )
    db.add(key)
    db.flush()
    return key, plaintext


def resolve_api_key(db: Session, plaintext: str) -> ApiKey | None:
    if not plaintext.startswith(security.API_KEY_PREFIX):
        return None
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == security.sha256_hex(plaintext)))
    if key is None or key.revoked_at is not None:
        return None
    now = utcnow()
    last = key.last_used_at
    if last is None or (now - (last if last.tzinfo else last.replace(tzinfo=timezone.utc))).total_seconds() > 60:
        key.last_used_at = now
        db.commit()
    return key


# --- queries ---------------------------------------------------------------


def queries_this_month(db: Session, org: Organization) -> int:
    return db.scalar(
        select(func.count(QueryLog.id)).where(
            QueryLog.organization_id == org.id,
            QueryLog.created_at >= month_start(),
            QueryLog.status == "ok",
        )
    ) or 0


def compile_for_model(db: Session, org: Organization, model: SemanticModel, payload: Any) -> CompiledQuery:
    spec = model_spec(db, model)
    plan = plan_for(org)
    query = parse_query(payload, max_rows=plan.max_rows)
    conn = db.get(Connection, model.connection_id) if model.connection_id else None
    if conn is not None:
        dialect = dialect_for(conn.kind)
    else:
        requested = payload.get("dialect") if isinstance(payload, dict) else None
        allowed = {k["dialect"] for k in KINDS.values()} | {"trino", "tsql", "spark"}
        if requested is not None and requested not in allowed:
            raise QueryError(f"Unknown dialect `{requested}`; use one of {', '.join(sorted(allowed))}")
        dialect = requested
    return compile_query(spec, query, dialect)


def run_query(
    db: Session,
    org: Organization,
    model: SemanticModel,
    payload: Any,
    *,
    source: str,
    user: User | None = None,
    api_key: ApiKey | None = None,
) -> dict[str, Any]:
    plan = plan_for(org)
    used = queries_this_month(db, org)
    _check_limit(
        plan.monthly_queries,
        used,
        f"You have used all {plan.monthly_queries:,} queries included in the {plan.name} plan this month. Upgrade for more.",
    )
    if model.connection_id is None:
        raise ServiceError("Attach a warehouse connection to this model before running queries (model settings).", 409)
    conn = db.get(Connection, model.connection_id)
    if conn is None:
        raise ServiceError("This model's connection was deleted; choose another in model settings.", 409)

    log = QueryLog(
        organization_id=org.id,
        semantic_model_id=model.id,
        user_id=user.id if user else None,
        api_key_id=api_key.id if api_key else None,
        source=source,
        request=json.dumps(payload, default=str)[:10_000],
    )
    start = time.perf_counter()
    try:
        compiled = compile_for_model(db, org, model, payload)
        log.sql = compiled.sql
        result = connector_for(conn).execute(compiled.sql)
    except (QueryError, ServiceError, ConnectorError) as e:
        log.status = "error"
        log.error = str(e)[:2000]
        log.duration_ms = int((time.perf_counter() - start) * 1000)
        db.add(log)
        db.commit()
        if isinstance(e, ServiceError):
            raise
        raise ServiceError(str(e), 400 if isinstance(e, QueryError) else 502) from e
    log.row_count = len(result.rows)
    log.duration_ms = int((time.perf_counter() - start) * 1000)
    db.add(log)
    db.commit()
    return {
        "columns": compiled.columns,
        "rows": result.rows,
        "row_count": len(result.rows),
        "sql": compiled.sql,
        "warnings": compiled.warnings,
        "plan": compiled.plan,
        "duration_ms": log.duration_ms,
        "model": model.slug,
        "model_version": model.current_version,
    }


def usage_summary(db: Session, org: Organization) -> dict[str, Any]:
    plan = plan_for(org)
    models = db.scalar(select(func.count(SemanticModel.id)).where(SemanticModel.organization_id == org.id)) or 0
    connections = db.scalar(
        select(func.count(Connection.id)).where(Connection.organization_id == org.id, Connection.kind != "demo")
    ) or 0
    keys = db.scalar(
        select(func.count(ApiKey.id)).where(ApiKey.organization_id == org.id, ApiKey.revoked_at.is_(None))
    ) or 0
    members = db.scalar(select(func.count(Membership.id)).where(Membership.organization_id == org.id)) or 0
    return {
        "plan": plan,
        "queries": queries_this_month(db, org),
        "models": models,
        "connections": connections,
        "api_keys": keys,
        "members": members,
    }
