"""REST API v1. Authenticate with `Authorization: Bearer so_live_...`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import security
from .db import get_db
from .engine.query import QueryError
from .models import ApiKey, Connection, Membership, Organization, User
from .services import (
    ServiceError,
    ValidationFailed,
    catalog,
    compile_for_model,
    create_model,
    current_version,
    find_model,
    list_models,
    model_spec,
    resolve_api_key,
    run_query,
    save_model,
    usage_summary,
)
from .spec import loader


class ApiError(Exception):
    def __init__(self, message: str, status_code: int = 400, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


@dataclass
class Principal:
    org: Organization
    api_key: ApiKey | None
    user: User | None
    db: Session


def api_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        key = resolve_api_key(db, auth[7:].strip())
        if key is None:
            raise ApiError("Invalid or revoked API key", 401)
        return Principal(org=key.organization, api_key=key, user=None, db=db)
    # Browser session (the web app calls the API too); require CSRF on writes.
    uid = request.session.get("uid")
    oid = request.session.get("oid")
    if uid and oid:
        member = db.scalar(select(Membership).where(Membership.user_id == uid, Membership.organization_id == oid))
        if member is not None:
            if request.method not in ("GET", "HEAD") and not security.csrf_matches(
                request.session.get("csrf"), request.headers.get("x-csrf-token")
            ):
                raise ApiError("Missing CSRF token", 403)
            return Principal(org=member.organization, api_key=None, user=member.user, db=db)
    raise ApiError("Authentication required: send `Authorization: Bearer <api key>`", 401)


def error_response(exc: ApiError) -> JSONResponse:
    body: dict[str, Any] = {"error": {"message": str(exc), "status": exc.status_code}}
    if exc.details is not None:
        body["error"]["details"] = exc.details
    return JSONResponse(body, status_code=exc.status_code)


router = APIRouter(prefix="/api/v1", tags=["API v1"])


def _service(call):
    try:
        return call()
    except ValidationFailed as e:
        raise ApiError(str(e), 422, details=[i.as_dict() for i in e.result.issues]) from e
    except ServiceError as e:
        raise ApiError(str(e), e.status_code) from e
    except QueryError as e:
        raise ApiError(str(e), 400) from e


def _model_summary(p: Principal, m) -> dict[str, Any]:
    conn = p.db.get(Connection, m.connection_id) if m.connection_id else None
    return {
        "slug": m.slug,
        "name": m.name,
        "description": m.description,
        "version": m.current_version,
        "connection": {"name": conn.name, "kind": conn.kind} if conn else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


@router.get("/models", summary="List semantic models")
def api_list_models(p: Principal = Depends(api_principal)):
    return {"models": [_model_summary(p, m) for m in list_models(p.db, p.org)]}


@router.post("/models", status_code=201, summary="Create a model from an Ossie YAML/JSON document")
def api_create_model(p: Principal = Depends(api_principal), payload: dict = Body(...)):
    content = payload.get("content")
    if isinstance(content, dict):
        content = loader.dump_yaml(content)
    if not isinstance(content, str):
        raise ApiError("`content` must be the model document (YAML/JSON text or a JSON object)")
    connection = payload.get("connection")
    connection_id = None
    if connection:
        conn = p.db.scalar(select(Connection).where(Connection.organization_id == p.org.id, Connection.name == connection))
        if conn is None:
            raise ApiError(f"Unknown connection `{connection}`", 404)
        connection_id = conn.id
    result = _service(lambda: create_model(p.db, p.org, p.user, content, connection_id=connection_id, message=payload.get("message") or "Created via API"))
    p.db.commit()
    return {"model": _model_summary(p, result.model), "warnings": [i.as_dict() for i in result.load.warnings], "notes": result.load.notes}


@router.get("/models/{slug}", summary="Describe a model: datasets, dimensions and metrics")
def api_get_model(slug: str, p: Principal = Depends(api_principal)):
    model = _service(lambda: find_model(p.db, p.org, slug))
    spec = _service(lambda: model_spec(p.db, model))
    return {"model": _model_summary(p, model), "catalog": catalog(spec)}


@router.get("/models/{slug}/document", summary="Download the current Ossie document")
def api_get_document(slug: str, format: str = "yaml", p: Principal = Depends(api_principal)):
    model = _service(lambda: find_model(p.db, p.org, slug))
    version = current_version(p.db, model)
    if version is None:
        raise ApiError("Model has no versions", 404)
    if format == "json":
        return JSONResponse(loader.parse_text(version.content))
    return PlainTextResponse(version.content, media_type="application/yaml")


@router.put("/models/{slug}/document", summary="Save a new version of the model")
async def api_put_document(slug: str, request: Request, p: Principal = Depends(api_principal)):
    model = _service(lambda: find_model(p.db, p.org, slug))
    raw = (await request.body()).decode("utf-8", errors="replace")
    message = request.headers.get("x-change-message", "Updated via API")
    content = raw
    if "json" in request.headers.get("content-type", ""):
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ApiError("Invalid JSON body") from e
        if isinstance(body, dict) and "content" in body:
            content = body["content"] if isinstance(body["content"], str) else loader.dump_yaml(body["content"])
            message = body.get("message") or message
    result = _service(lambda: save_model(p.db, model, p.user, content, message))
    p.db.commit()
    return {
        "model": _model_summary(p, model),
        "changed": result.changed,
        "warnings": [i.as_dict() for i in result.load.warnings],
    }


@router.post("/validate", summary="Validate an Ossie document without saving it")
async def api_validate(request: Request, p: Principal = Depends(api_principal)):
    raw = (await request.body()).decode("utf-8", errors="replace")
    if "json" in request.headers.get("content-type", ""):
        try:
            body = json.loads(raw)
            if isinstance(body, dict) and "content" in body:
                raw = body["content"] if isinstance(body["content"], str) else loader.dump_yaml(body["content"])
        except json.JSONDecodeError:
            pass
    result = loader.load(raw)
    return {
        "valid": not result.errors and result.document is not None,
        "issues": [i.as_dict() for i in result.issues],
        "notes": result.notes,
        "migrated": loader.dump_yaml(result.document) if result.notes and result.document else None,
    }


@router.post("/models/{slug}/compile", summary="Compile a semantic query to SQL without running it")
def api_compile(slug: str, query: dict = Body(...), p: Principal = Depends(api_principal)):
    model = _service(lambda: find_model(p.db, p.org, slug))
    compiled = _service(lambda: compile_for_model(p.db, p.org, model, query))
    return {"sql": compiled.sql, "columns": compiled.columns, "warnings": compiled.warnings, "plan": compiled.plan}


@router.post("/models/{slug}/query", summary="Run a semantic query and return rows")
def api_query(slug: str, query: dict = Body(...), p: Principal = Depends(api_principal)):
    model = _service(lambda: find_model(p.db, p.org, slug))
    return _service(
        lambda: run_query(p.db, p.org, model, query, source="api", user=p.user, api_key=p.api_key)
    )


@router.get("/usage", summary="Plan limits and usage this month")
def api_usage(p: Principal = Depends(api_principal)):
    usage = usage_summary(p.db, p.org)
    plan = usage.pop("plan")
    return {
        "workspace": p.org.slug,
        "plan": plan.key,
        "limits": {
            "monthly_queries": plan.monthly_queries,
            "models": plan.max_models,
            "connections": plan.max_connections,
            "api_keys": plan.max_api_keys,
            "max_rows": plan.max_rows,
        },
        "usage": usage,
    }
