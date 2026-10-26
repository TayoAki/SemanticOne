"""Shared helpers for HTML routes: templates, sessions, CSRF, flash messages and auth."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import security
from ..config import get_settings
from ..db import get_db
from ..models import Membership, Organization, User
from ..plans import PLANS, fmt_limit
from ..services import plan_for

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["fmt_limit"] = fmt_limit
templates.env.globals["PLANS"] = PLANS


def _datetime(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "—"


templates.env.filters["dt"] = _datetime


class LoginRequired(Exception):
    pass


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = security.new_csrf_token()
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    """Dependency for state-changing routes: form field or X-CSRF-Token header must match the session."""
    provided = request.headers.get("x-csrf-token")
    if not provided:
        content_type = request.headers.get("content-type", "")
        if "form" in content_type:
            form = await request.form()
            provided = form.get("csrf_token")  # type: ignore[assignment]
    if not security.csrf_matches(request.session.get("csrf"), provided):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Reload the page and try again.")


def flash(request: Request, message: str, category: str = "info") -> None:
    messages = request.session.get("flash", [])
    messages.append({"message": message, "category": category})
    request.session["flash"] = messages[-5:]


def pop_flash(request: Request) -> list[dict[str, str]]:
    return request.session.pop("flash", [])


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def login_user(request: Request, user: User, org_id: int | None) -> None:
    request.session.clear()
    request.session["uid"] = user.id
    if org_id is not None:
        request.session["oid"] = org_id
    request.session["csrf"] = security.new_csrf_token()


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return user


def require_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise LoginRequired()
    return user


@dataclass
class Ctx:
    """The signed-in user, their current workspace and role."""

    user: User
    org: Organization
    membership: Membership
    db: Session

    @property
    def is_admin(self) -> bool:
        return self.membership.is_admin

    def require_admin(self) -> None:
        if not self.is_admin:
            raise HTTPException(status_code=403, detail="Only workspace admins can do this.")


def is_platform_admin(user: User | None) -> bool:
    return user is not None and user.email.lower() in get_settings().admin_emails


def require_platform_admin(user: User = Depends(require_user)) -> User:
    if not is_platform_admin(user):
        raise HTTPException(status_code=404)
    return user


def require_ctx(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)) -> Ctx:
    memberships = list(db.scalars(select(Membership).where(Membership.user_id == user.id).order_by(Membership.id)))
    if not memberships:
        raise HTTPException(status_code=403, detail="You are not a member of any workspace.")
    oid = request.session.get("oid")
    membership = next((m for m in memberships if m.organization_id == oid), memberships[0])
    request.session["oid"] = membership.organization_id
    return Ctx(user=user, org=membership.organization, membership=membership, db=db)


def render(request: Request, template: str, ctx: Ctx | None = None, status_code: int = 200, **context: Any) -> HTMLResponse:
    settings = get_settings()
    base: dict[str, Any] = {
        "request": request,
        "settings": settings,
        "csrf_token": csrf_token(request),
        "flashes": pop_flash(request),
        "user": ctx.user if ctx else context.pop("user", None),
    }
    base["is_platform_admin"] = is_platform_admin(base["user"])
    if ctx is not None:
        base.update(
            {
                "org": ctx.org,
                "membership": ctx.membership,
                "plan": plan_for(ctx.org),
                "memberships": list(
                    ctx.db.scalars(select(Membership).where(Membership.user_id == ctx.user.id).order_by(Membership.id))
                ),
            }
        )
    base.update(context)
    return templates.TemplateResponse(request, template, base, status_code=status_code)
