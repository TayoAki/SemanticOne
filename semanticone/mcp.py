"""Model Context Protocol server (Streamable HTTP transport, JSON responses).

AI agents (Claude, ChatGPT, Cursor, custom agents) connect to `POST /mcp` with an
API key and get tools to discover governed metrics and query them safely.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from . import __version__
from .db import get_db
from .models import ApiKey, Connection
from .services import (
    ServiceError,
    catalog,
    compile_for_model,
    find_model,
    list_models,
    model_spec,
    resolve_api_key,
    run_query,
)
from .engine.query import FILTER_OPS, GRAINS, QueryError
from .spec import loader

router = APIRouter()

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "SemanticOne exposes governed business metrics defined in Apache Ossie semantic models. "
    "Call list_models, then describe_model to see metric and dimension names and their meanings, "
    "then query_metrics. Always use metric definitions instead of writing your own SQL; the engine "
    "prevents double counting across joins and refuses queries it cannot answer correctly."
)

_QUERY_PROPERTIES: dict[str, Any] = {
    "model": {"type": "string", "description": "Model slug from list_models"},
    "metrics": {"type": "array", "items": {"type": "string"}, "description": "Metric names from describe_model"},
    "dimensions": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Dimension names as `dataset.field`; append `__month` etc. to time dimensions "
        f"({', '.join(GRAINS)})",
    },
    "filters": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "description": "`dataset.field`, or a requested metric name"},
                "op": {"type": "string", "enum": list(FILTER_OPS)},
                "value": {"description": "Scalar, or a list for in/not_in/between"},
            },
            "required": ["field", "op"],
        },
    },
    "order_by": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Requested dimension/metric names; prefix with `-` for descending",
    },
    "limit": {"type": "integer", "minimum": 1, "default": 100},
}

TOOLS = [
    {
        "name": "list_models",
        "title": "List semantic models",
        "description": "List the semantic models (business domains) available in this workspace.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "describe_model",
        "title": "Describe a semantic model",
        "description": "Get a model's metrics, dimensions, datasets and relationships, including descriptions, "
        "synonyms and usage instructions. Call before querying.",
        "inputSchema": {"type": "object", "properties": {"model": _QUERY_PROPERTIES["model"]}, "required": ["model"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "query_metrics",
        "title": "Query metrics",
        "description": "Compute metrics grouped by dimensions with optional filters. Returns rows and the SQL that ran.",
        "inputSchema": {"type": "object", "properties": _QUERY_PROPERTIES, "required": ["model"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "compile_sql",
        "title": "Compile a query to SQL",
        "description": "Return the SQL for a metrics query without running it.",
        "inputSchema": {"type": "object", "properties": _QUERY_PROPERTIES, "required": ["model"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "validate_model",
        "title": "Validate an Ossie model",
        "description": "Validate an Apache Ossie semantic model document (YAML or JSON) and list errors and warnings.",
        "inputSchema": {
            "type": "object",
            "properties": {"content": {"type": "string", "description": "The model document"}},
            "required": ["content"],
        },
        "annotations": {"readOnlyHint": True},
    },
]


def _rpc_error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _rpc_result(id_: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _tool_result(data: Any, is_error: bool = False) -> dict:
    text = data if isinstance(data, str) else json.dumps(data, default=str, indent=1)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if isinstance(data, dict) and not is_error:
        result["structuredContent"] = data
    return result


def _query_payload(args: dict) -> dict:
    payload = {k: args[k] for k in ("metrics", "dimensions", "filters", "order_by", "limit") if k in args}
    payload.setdefault("limit", 100)
    return payload


def call_tool(db: Session, key: ApiKey, name: str, args: dict) -> dict:
    org = key.organization
    try:
        if name == "list_models":
            models = []
            for m in list_models(db, org):
                conn = db.get(Connection, m.connection_id) if m.connection_id else None
                models.append(
                    {
                        "model": m.slug,
                        "name": m.name,
                        "description": m.description,
                        "queryable": conn is not None,
                    }
                )
            return _tool_result({"models": models})
        if name == "describe_model":
            model = find_model(db, org, str(args.get("model", "")))
            return _tool_result(catalog(model_spec(db, model)))
        if name == "query_metrics":
            model = find_model(db, org, str(args.get("model", "")))
            result = run_query(db, org, model, _query_payload(args), source="mcp", api_key=key)
            result.pop("plan", None)
            return _tool_result(result)
        if name == "compile_sql":
            model = find_model(db, org, str(args.get("model", "")))
            compiled = compile_for_model(db, org, model, _query_payload(args))
            return _tool_result({"sql": compiled.sql, "columns": compiled.columns, "warnings": compiled.warnings})
        if name == "validate_model":
            result = loader.load(str(args.get("content", "")))
            return _tool_result(
                {"valid": not result.errors, "issues": [i.as_dict() for i in result.issues], "notes": result.notes}
            )
    except (ServiceError, QueryError) as e:
        return _tool_result(str(e), is_error=True)
    raise KeyError(name)


def handle_message(db: Session, key: ApiKey, message: Any) -> dict | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    method = message["method"]
    id_ = message.get("id")
    params = message.get("params") or {}
    if id_ is None:  # notification
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return _rpc_result(
            id_,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "semanticone", "title": "SemanticOne", "version": __version__},
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _rpc_result(id_, {})
    if method == "tools/list":
        return _rpc_result(id_, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _rpc_error(id_, -32602, "`arguments` must be an object")
        try:
            return _rpc_result(id_, call_tool(db, key, str(name), args))
        except KeyError:
            return _rpc_error(id_, -32602, f"Unknown tool: {name}")
    if method in ("resources/list", "prompts/list"):
        return _rpc_result(id_, {method.split("/")[0]: []})
    return _rpc_error(id_, -32601, f"Method not found: {method}")


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": "Missing or invalid API key. Send `Authorization: Bearer so_live_...`."},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="semanticone"'},
    )


@router.post("/mcp")
async def mcp_post(request: Request, db: Session = Depends(get_db)):
    auth = request.headers.get("authorization", "")
    key = resolve_api_key(db, auth[7:].strip()) if auth.lower().startswith("bearer ") else None
    if key is None:
        return _unauthorized()
    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)
    if isinstance(body, list):
        responses = [r for r in (handle_message(db, key, m) for m in body) if r is not None]
        return JSONResponse(responses) if responses else Response(status_code=202)
    response = handle_message(db, key, body)
    if response is None:
        return Response(status_code=202)
    return JSONResponse(response)


@router.get("/mcp")
@router.delete("/mcp")
def mcp_other():
    # This server does not open server-initiated SSE streams or keep sessions.
    return Response(status_code=405, headers={"Allow": "POST"})
