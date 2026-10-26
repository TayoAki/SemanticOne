"""Signed-in application pages under /app."""

from __future__ import annotations

import difflib
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import func, select
from starlette.datastructures import UploadFile

from .. import security
from ..config import get_settings
from ..engine.connectors import KINDS
from ..engine.demo import DEMO_MODEL_YAML
from ..mailer import send_email
from ..models import ROLES, ROLE_OWNER, ApiKey, Connection, Invite, Membership, ModelVersion, QueryLog, utcnow
from ..services import (
    LimitError,
    ServiceError,
    ValidationFailed,
    _check_limit,
    catalog,
    create_api_key,
    create_connection,
    create_model,
    current_version,
    delete_connection,
    find_model,
    get_version,
    list_models,
    model_spec,
    plan_for,
    redacted_url,
    run_query,
    save_model,
    test_connection,
    usage_summary,
)
from ..spec import loader
from .deps import Ctx, flash, redirect, render, require_ctx, verify_csrf

router = APIRouter(prefix="/app")

BLANK_MODEL = """\
version: 0.2.0.dev0
name: My semantic model
description: Describe what this model covers.
datasets:
  - name: orders
    source: analytics.public.orders
    primary_key: [order_id]
    fields:
      - name: order_id
        expression: {sql: id}
        datatype: Integer
      - name: ordered_at
        expression: {sql: created_at}
        datatype: DateTime
        dimension: {is_time: true}
      - name: amount
        expression: {sql: amount_cents / 100.0}
        datatype: Decimal
metrics:
  - name: revenue
    expression: {sql: SUM(orders.amount)}
    datatype: Decimal
"""


# --- dashboard -------------------------------------------------------------


@router.get("")
def dashboard(request: Request, ctx: Ctx = Depends(require_ctx)):
    db = ctx.db
    models = list_models(db, ctx.org)
    recent = list(
        db.scalars(
            select(QueryLog).where(QueryLog.organization_id == ctx.org.id).order_by(QueryLog.id.desc()).limit(8)
        )
    )
    model_names = {m.id: m for m in models}
    return render(
        request,
        "app/dashboard.html",
        ctx,
        models=models,
        usage=usage_summary(db, ctx.org),
        recent=recent,
        model_names=model_names,
    )


@router.post("/switch/{org_id}", dependencies=[Depends(verify_csrf)])
def switch_org(request: Request, org_id: int, ctx: Ctx = Depends(require_ctx)):
    member = ctx.db.scalar(
        select(Membership).where(Membership.user_id == ctx.user.id, Membership.organization_id == org_id)
    )
    if member is None:
        raise HTTPException(404)
    request.session["oid"] = org_id
    return redirect("/app")


# --- models ----------------------------------------------------------------


@router.get("/models/new")
def new_model_form(request: Request, ctx: Ctx = Depends(require_ctx), template: str = "blank"):
    content = DEMO_MODEL_YAML if template == "sample" else BLANK_MODEL
    connections = _connections(ctx)
    return render(request, "app/model_new.html", ctx, content=content, connections=connections, issues=[], notes=[])


@router.post("/models/new", dependencies=[Depends(verify_csrf)])
async def new_model(request: Request, ctx: Ctx = Depends(require_ctx)):
    form = await request.form()
    content = str(form.get("content") or "")
    upload = form.get("file")
    if isinstance(upload, UploadFile) and upload.filename:
        raw = await upload.read()
        if len(raw) > 5_000_000:
            content = ""
            flash(request, "Files must be under 5 MB.", "error")
        else:
            content = raw.decode("utf-8", errors="replace")
    connection_id = _int_or_none(form.get("connection_id"))
    try:
        result = create_model(ctx.db, ctx.org, ctx.user, content, connection_id=connection_id)
        ctx.db.commit()
    except ValidationFailed as e:
        ctx.db.rollback()
        return render(
            request,
            "app/model_new.html",
            ctx,
            content=content,
            connections=_connections(ctx),
            issues=e.result.issues,
            notes=e.result.notes,
            status_code=422,
        )
    except ServiceError as e:
        ctx.db.rollback()
        flash(request, str(e), "error")
        return redirect("/app/billing" if isinstance(e, LimitError) else "/app/models/new")
    for note in result.load.notes:
        flash(request, note, "info")
    flash(request, f"Created model {result.model.name}.", "success")
    return redirect(f"/app/models/{result.model.slug}")


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value not in (None, "", "none") else None
    except (TypeError, ValueError):
        return None


def _connections(ctx: Ctx) -> list[Connection]:
    return list(
        ctx.db.scalars(select(Connection).where(Connection.organization_id == ctx.org.id).order_by(Connection.name))
    )


def _model(ctx: Ctx, slug: str):
    try:
        return find_model(ctx.db, ctx.org, slug)
    except ServiceError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/models/{slug}")
def model_overview(request: Request, slug: str, ctx: Ctx = Depends(require_ctx)):
    model = _model(ctx, slug)
    version = current_version(ctx.db, model)
    result = loader.load(version.content) if version else None
    spec = result.model() if result and not result.errors else None
    return render(
        request,
        "app/model_overview.html",
        ctx,
        model=model,
        version=version,
        spec=spec,
        issues=result.issues if result else [],
        catalog=catalog(spec) if spec else None,
        tab="overview",
    )


@router.get("/models/{slug}/edit")
def model_edit_form(request: Request, slug: str, ctx: Ctx = Depends(require_ctx)):
    model = _model(ctx, slug)
    version = current_version(ctx.db, model)
    content = version.content if version else BLANK_MODEL
    issues = loader.load(content).issues
    return render(request, "app/model_edit.html", ctx, model=model, content=content, issues=issues, tab="edit")


@router.post("/models/{slug}/edit", dependencies=[Depends(verify_csrf)])
def model_edit(
    request: Request,
    slug: str,
    content: str = Form(...),
    message: str = Form(""),
    ctx: Ctx = Depends(require_ctx),
):
    model = _model(ctx, slug)
    try:
        result = save_model(ctx.db, model, ctx.user, content, message)
        ctx.db.commit()
    except ValidationFailed as e:
        ctx.db.rollback()
        return render(
            request,
            "app/model_edit.html",
            ctx,
            model=model,
            content=content,
            issues=e.result.issues,
            message=message,
            tab="edit",
            status_code=422,
        )
    if result.changed:
        flash(request, f"Saved version {model.current_version}.", "success")
    else:
        flash(request, "No changes to save.", "info")
    return redirect(f"/app/models/{model.slug}")


@router.post("/models/{slug}/validate", dependencies=[Depends(verify_csrf)])
async def model_validate(request: Request, slug: str, ctx: Ctx = Depends(require_ctx)):
    _model(ctx, slug)
    body = await request.json()
    result = loader.load(str(body.get("content", "")))
    return JSONResponse(
        {"ok": not result.errors, "issues": [i.as_dict() for i in result.issues], "notes": result.notes}
    )


@router.get("/models/{slug}/explore")
def model_explore(request: Request, slug: str, ctx: Ctx = Depends(require_ctx)):
    model = _model(ctx, slug)
    try:
        spec = model_spec(ctx.db, model)
    except ServiceError as e:
        flash(request, str(e), "error")
        return redirect(f"/app/models/{slug}")
    return render(
        request,
        "app/model_explore.html",
        ctx,
        model=model,
        catalog=catalog(spec),
        connection=ctx.db.get(Connection, model.connection_id) if model.connection_id else None,
        tab="explore",
    )


@router.post("/models/{slug}/query", dependencies=[Depends(verify_csrf)])
async def model_query(request: Request, slug: str, ctx: Ctx = Depends(require_ctx)):
    model = _model(ctx, slug)
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Request body must be JSON"}, status_code=400)
    try:
        result = run_query(ctx.db, ctx.org, model, payload, source="web", user=ctx.user)
    except ServiceError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status_code)
    return JSONResponse(result)


@router.get("/models/{slug}/versions")
def model_versions(request: Request, slug: str, ctx: Ctx = Depends(require_ctx)):
    model = _model(ctx, slug)
    versions = list(
        ctx.db.scalars(
            select(ModelVersion).where(ModelVersion.semantic_model_id == model.id).order_by(ModelVersion.version.desc())
        )
    )
    return render(request, "app/model_versions.html", ctx, model=model, versions=versions, tab="versions")


@router.get("/models/{slug}/versions/{number}")
def model_version(request: Request, slug: str, number: int, ctx: Ctx = Depends(require_ctx), compare: int | None = None):
    model = _model(ctx, slug)
    version = get_version(ctx.db, model, number)
    if version is None:
        raise HTTPException(404)
    other_number = compare if compare is not None else number - 1
    other = get_version(ctx.db, model, other_number) if other_number >= 1 else None
    diff = []
    if other is not None:
        diff = list(
            difflib.unified_diff(
                other.content.splitlines(),
                version.content.splitlines(),
                fromfile=f"v{other.version}",
                tofile=f"v{version.version}",
                lineterm="",
            )
        )
    return render(
        request, "app/model_version.html", ctx, model=model, version=version, other=other, diff=diff, tab="versions"
    )


@router.post("/models/{slug}/versions/{number}/restore", dependencies=[Depends(verify_csrf)])
def model_restore(request: Request, slug: str, number: int, ctx: Ctx = Depends(require_ctx)):
    model = _model(ctx, slug)
    version = get_version(ctx.db, model, number)
    if version is None:
        raise HTTPException(404)
    try:
        save_model(ctx.db, model, ctx.user, version.content, f"Restored version {number}")
        ctx.db.commit()
    except ServiceError as e:
        ctx.db.rollback()
        flash(request, str(e), "error")
        return redirect(f"/app/models/{slug}/versions")
    flash(request, f"Restored version {number} as version {model.current_version}.", "success")
    return redirect(f"/app/models/{slug}")


@router.get("/models/{slug}/download")
def model_download(slug: str, ctx: Ctx = Depends(require_ctx), format: str = "yaml", version: int | None = None):
    model = _model(ctx, slug)
    v = get_version(ctx.db, model, version) if version else current_version(ctx.db, model)
    if v is None:
        raise HTTPException(404)
    if format == "json":
        doc = loader.parse_text(v.content)
        body = loader.dump_json(doc)
        media = "application/json"
    else:
        body, media = v.content, "application/yaml"
    filename = f"{model.slug}.v{v.version}.{'json' if format == 'json' else 'yaml'}"
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/models/{slug}/settings")
def model_settings(request: Request, slug: str, ctx: Ctx = Depends(require_ctx)):
    model = _model(ctx, slug)
    return render(request, "app/model_settings.html", ctx, model=model, connections=_connections(ctx), tab="settings")


@router.post("/models/{slug}/settings", dependencies=[Depends(verify_csrf)])
def model_settings_save(
    request: Request, slug: str, connection_id: str = Form(""), ctx: Ctx = Depends(require_ctx)
):
    model = _model(ctx, slug)
    cid = _int_or_none(connection_id)
    if cid is not None:
        conn = ctx.db.get(Connection, cid)
        if conn is None or conn.organization_id != ctx.org.id:
            raise HTTPException(404)
    model.connection_id = cid
    ctx.db.commit()
    flash(request, "Model settings saved.", "success")
    return redirect(f"/app/models/{slug}/settings")


@router.post("/models/{slug}/delete", dependencies=[Depends(verify_csrf)])
def model_delete(request: Request, slug: str, ctx: Ctx = Depends(require_ctx)):
    ctx.require_admin()
    model = _model(ctx, slug)
    ctx.db.delete(model)
    ctx.db.commit()
    flash(request, f"Deleted model {model.name}.", "success")
    return redirect("/app")


# --- connections -----------------------------------------------------------


@router.get("/connections")
def connections_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    conns = _connections(ctx)
    return render(
        request,
        "app/connections.html",
        ctx,
        connections=conns,
        kinds=KINDS,
        redacted={c.id: redacted_url(c) for c in conns},
    )


@router.post("/connections", dependencies=[Depends(verify_csrf)])
def connections_create(
    request: Request,
    name: str = Form(...),
    kind: str = Form(...),
    url: str = Form(""),
    ctx: Ctx = Depends(require_ctx),
):
    ctx.require_admin()
    try:
        conn = create_connection(ctx.db, ctx.org, name, kind, url)
        ctx.db.commit()
    except ServiceError as e:
        ctx.db.rollback()
        flash(request, str(e), "error")
        return redirect("/app/billing" if isinstance(e, LimitError) else "/app/connections")
    try:
        message = test_connection(conn)
        flash(request, f"Added {conn.name}. {message}", "success")
    except ServiceError as e:
        flash(request, f"Added {conn.name}, but the connection test failed: {e}", "error")
    return redirect("/app/connections")


@router.post("/connections/{conn_id}/test", dependencies=[Depends(verify_csrf)])
def connections_test(request: Request, conn_id: int, ctx: Ctx = Depends(require_ctx)):
    conn = _connection(ctx, conn_id)
    try:
        flash(request, f"{conn.name}: {test_connection(conn)}", "success")
    except ServiceError as e:
        flash(request, f"{conn.name}: {e}", "error")
    return redirect("/app/connections")


@router.post("/connections/{conn_id}/delete", dependencies=[Depends(verify_csrf)])
def connections_delete(request: Request, conn_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.require_admin()
    conn = _connection(ctx, conn_id)
    delete_connection(ctx.db, conn)
    ctx.db.commit()
    flash(request, f"Deleted connection {conn.name}. Models that used it need a new connection.", "success")
    return redirect("/app/connections")


def _connection(ctx: Ctx, conn_id: int) -> Connection:
    conn = ctx.db.get(Connection, conn_id)
    if conn is None or conn.organization_id != ctx.org.id:
        raise HTTPException(404)
    return conn


# --- API keys & integrations -----------------------------------------------


@router.get("/api-keys")
def api_keys_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    keys = list(
        ctx.db.scalars(select(ApiKey).where(ApiKey.organization_id == ctx.org.id).order_by(ApiKey.created_at.desc()))
    )
    new_key = request.session.pop("new_api_key", None)
    models = list_models(ctx.db, ctx.org)
    return render(request, "app/api_keys.html", ctx, keys=keys, new_key=new_key, models=models)


@router.post("/api-keys", dependencies=[Depends(verify_csrf)])
def api_keys_create(request: Request, name: str = Form("API key"), ctx: Ctx = Depends(require_ctx)):
    ctx.require_admin()
    try:
        _, plaintext = create_api_key(ctx.db, ctx.org, ctx.user, name)
        ctx.db.commit()
    except ServiceError as e:
        ctx.db.rollback()
        flash(request, str(e), "error")
        return redirect("/app/api-keys")
    request.session["new_api_key"] = plaintext
    return redirect("/app/api-keys")


@router.post("/api-keys/{key_id}/revoke", dependencies=[Depends(verify_csrf)])
def api_keys_revoke(request: Request, key_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.require_admin()
    key = ctx.db.get(ApiKey, key_id)
    if key is None or key.organization_id != ctx.org.id:
        raise HTTPException(404)
    key.revoked_at = utcnow()
    ctx.db.commit()
    flash(request, f"Revoked {key.name}.", "success")
    return redirect("/app/api-keys")


# --- usage -----------------------------------------------------------------


@router.get("/usage")
def usage_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    logs = list(
        ctx.db.scalars(
            select(QueryLog).where(QueryLog.organization_id == ctx.org.id).order_by(QueryLog.id.desc()).limit(100)
        )
    )
    models = {m.id: m for m in list_models(ctx.db, ctx.org)}
    return render(request, "app/usage.html", ctx, logs=logs, usage=usage_summary(ctx.db, ctx.org), models=models)


@router.get("/usage/{log_id}")
def usage_detail(log_id: int, ctx: Ctx = Depends(require_ctx)):
    log = ctx.db.get(QueryLog, log_id)
    if log is None or log.organization_id != ctx.org.id:
        raise HTTPException(404)
    return PlainTextResponse(f"-- request\n-- {log.request}\n\n{log.sql or '-- (not compiled)'}\n\n-- {log.error}")


# --- team & workspace settings ---------------------------------------------


@router.get("/settings")
def settings_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    members = list(
        ctx.db.scalars(select(Membership).where(Membership.organization_id == ctx.org.id).order_by(Membership.id))
    )
    invites = list(
        ctx.db.scalars(
            select(Invite).where(Invite.organization_id == ctx.org.id, Invite.accepted_at.is_(None)).order_by(Invite.id)
        )
    )
    invite_link = request.session.pop("invite_link", None)
    return render(
        request, "app/settings.html", ctx, members=members, invites=invites, roles=ROLES, invite_link=invite_link
    )


@router.post("/settings", dependencies=[Depends(verify_csrf)])
def settings_save(request: Request, name: str = Form(...), ctx: Ctx = Depends(require_ctx)):
    ctx.require_admin()
    if name.strip():
        ctx.org.name = name.strip()[:200]
        ctx.db.commit()
        flash(request, "Workspace renamed.", "success")
    return redirect("/app/settings")


@router.post("/settings/invites", dependencies=[Depends(verify_csrf)])
def invite_create(request: Request, email: str = Form(...), role: str = Form("member"), ctx: Ctx = Depends(require_ctx)):
    ctx.require_admin()
    plan = plan_for(ctx.org)
    members = ctx.db.scalar(select(func.count(Membership.id)).where(Membership.organization_id == ctx.org.id)) or 0
    pending = ctx.db.scalar(
        select(func.count(Invite.id)).where(Invite.organization_id == ctx.org.id, Invite.accepted_at.is_(None))
    ) or 0
    try:
        _check_limit(plan.max_members, members + pending, f"Your {plan.name} plan includes {plan.max_members} seats.")
    except LimitError as e:
        flash(request, str(e), "error")
        return redirect("/app/billing")
    email = email.strip().lower()
    if "@" not in email:
        flash(request, "Enter a valid email address.", "error")
        return redirect("/app/settings")
    if role not in ROLES or role == ROLE_OWNER:
        role = "member"
    token, token_hash = security.generate_token()
    ctx.db.add(Invite(organization_id=ctx.org.id, email=email, role=role, token_hash=token_hash, invited_by_id=ctx.user.id))
    ctx.db.commit()
    link = f"{get_settings().base_url}/invite/{token}"
    sent = send_email(
        email,
        f"{ctx.user.name or ctx.user.email} invited you to {ctx.org.name} on SemanticOne",
        f"Join the {ctx.org.name} workspace on SemanticOne:\n\n{link}\n",
    )
    request.session["invite_link"] = link
    flash(request, f"Invitation created for {email}." + ("" if sent else " Email is not configured, so share the link below."), "success")
    return redirect("/app/settings")


@router.post("/settings/invites/{invite_id}/delete", dependencies=[Depends(verify_csrf)])
def invite_delete(request: Request, invite_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.require_admin()
    invite = ctx.db.get(Invite, invite_id)
    if invite is None or invite.organization_id != ctx.org.id:
        raise HTTPException(404)
    ctx.db.delete(invite)
    ctx.db.commit()
    return redirect("/app/settings")


@router.post("/settings/members/{member_id}/remove", dependencies=[Depends(verify_csrf)])
def member_remove(request: Request, member_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.require_admin()
    member = ctx.db.get(Membership, member_id)
    if member is None or member.organization_id != ctx.org.id:
        raise HTTPException(404)
    if member.role == ROLE_OWNER:
        flash(request, "The workspace owner cannot be removed.", "error")
        return redirect("/app/settings")
    ctx.db.delete(member)
    ctx.db.commit()
    flash(request, "Member removed.", "success")
    return redirect("/app/settings")
