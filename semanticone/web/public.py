"""Marketing pages and authentication."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import security
from ..config import get_settings
from ..db import get_db
from ..mailer import send_email
from ..models import Invite, Membership, Organization, SalesLead, User, utcnow
from ..plans import PLANS
from ..services import ServiceError, authenticate, create_organization, create_user, memberships_for
from .deps import flash, login_user, optional_user, redirect, render, verify_csrf

router = APIRouter()


@router.get("/")
def landing(request: Request, user: User | None = Depends(optional_user)):
    return render(request, "landing.html", user=user)


@router.get("/pricing")
def pricing(request: Request, user: User | None = Depends(optional_user)):
    return render(request, "pricing.html", user=user, plans=list(PLANS.values()))


@router.get("/docs")
def docs(request: Request, user: User | None = Depends(optional_user)):
    return render(request, "docs.html", user=user)


@router.get("/legal/terms")
def terms(request: Request, user: User | None = Depends(optional_user)):
    return render(request, "legal_terms.html", user=user)


@router.get("/legal/privacy")
def privacy(request: Request, user: User | None = Depends(optional_user)):
    return render(request, "legal_privacy.html", user=user)


# --- contact sales -----------------------------------------------------------

contact_throttle = security.LoginThrottle(max_failures=5, window_seconds=3600)


@router.get("/contact")
def contact_form(request: Request, plan: str = "", workspace: str = "", user: User | None = Depends(optional_user)):
    return render(
        request,
        "contact.html",
        user=user,
        plan=plan if plan in PLANS else "",
        workspace=workspace,
        plans=list(PLANS.values()),
        form={"email": user.email if user else "", "name": user.name if user else ""},
    )


@router.post("/contact", dependencies=[Depends(verify_csrf)])
def contact_submit(
    request: Request,
    name: str = Form(""),
    email: str = Form(...),
    company: str = Form(""),
    plan: str = Form(""),
    message: str = Form(""),
    workspace: str = Form(""),
    website: str = Form(""),  # honeypot: humans never see this field
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
):
    if website:
        return render(request, "contact_thanks.html", user=user)
    key = request.client.host if request.client else "?"
    if contact_throttle.blocked(key):
        flash(request, "Too many requests. Please try again later.", "error")
        return redirect("/contact")
    email = email.strip().lower()
    if "@" not in email or len(email) > 320:
        return render(
            request, "contact.html", user=user, plan=plan, workspace=workspace, plans=list(PLANS.values()),
            form={"name": name, "email": email, "company": company, "message": message},
            error="Enter a valid email address.", status_code=400,
        )
    org = db.scalar(select(Organization).where(Organization.slug == workspace)) if workspace else None
    if org is not None and (user is None or not any(m.organization_id == org.id for m in user.memberships)):
        org = None  # only link a lead to a workspace the requester belongs to
    lead = SalesLead(
        name=name.strip()[:200],
        email=email,
        company=company.strip()[:200],
        plan_interest=plan if plan in PLANS else "",
        message=message.strip()[:5000],
        organization_id=org.id if org else None,
    )
    db.add(lead)
    db.commit()
    contact_throttle.failed(key)  # counts submissions per client
    settings = get_settings()
    if settings.sales_email and not settings.sales_email.endswith("@example.com"):
        send_email(
            settings.sales_email,
            f"New SemanticOne lead: {lead.company or lead.email} ({lead.plan_interest or 'general'})",
            f"{lead.name} <{lead.email}>\nCompany: {lead.company}\nPlan: {lead.plan_interest}\n"
            f"Workspace: {org.slug if org else '-'}\n\n{lead.message}\n\n{settings.base_url}/admin/leads",
        )
    return render(request, "contact_thanks.html", user=user)


# --- signup / login ----------------------------------------------------------


def _pending_invite(db: Session, token: str | None) -> Invite | None:
    if not token:
        return None
    invite = db.scalar(select(Invite).where(Invite.token_hash == security.sha256_hex(token)))
    if invite is None or invite.accepted_at is not None:
        return None
    return invite


@router.get("/signup")
def signup_form(request: Request, invite: str | None = None, user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    if user is not None and not invite:
        return redirect("/app")
    pending = _pending_invite(db, invite)
    if not get_settings().signups_enabled and pending is None:
        return render(request, "auth_closed.html", user=None)
    return render(request, "signup.html", user=None, invite=pending, invite_token=invite or "", form={})


@router.post("/signup", dependencies=[Depends(verify_csrf)])
def signup(
    request: Request,
    name: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
    workspace: str = Form(""),
    invite_token: str = Form(""),
    db: Session = Depends(get_db),
):
    pending = _pending_invite(db, invite_token)
    if not get_settings().signups_enabled and pending is None:
        return render(request, "auth_closed.html", user=None)
    form = {"name": name, "email": email, "workspace": workspace}
    try:
        user = create_user(db, email, password, name)
        if pending is not None and pending.email.lower() == user.email:
            db.add(Membership(user_id=user.id, organization_id=pending.organization_id, role=pending.role))
            pending.accepted_at = utcnow()
            org_id = pending.organization_id
        else:
            org = create_organization(db, user, workspace or (f"{name.split()[0]}'s workspace" if name.strip() else "My workspace"))
            org_id = org.id
        db.commit()
    except ServiceError as e:
        db.rollback()
        return render(request, "signup.html", user=None, error=str(e), invite=pending, invite_token=invite_token, form=form, status_code=400)
    login_user(request, user, org_id)
    flash(request, "Welcome to SemanticOne! We loaded a sample model and warehouse so you can try queries right away.", "success")
    return redirect("/app")


@router.get("/login")
def login_form(request: Request, next: str = "/app", user: User | None = Depends(optional_user)):
    if user is not None:
        return redirect("/app")
    return render(request, "login.html", user=None, next=next if next.startswith("/") and not next.startswith("//") else "/app")


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/app"),
    db: Session = Depends(get_db),
):
    throttle_key = f"{request.client.host if request.client else '?'}|{email.strip().lower()}"
    if security.login_throttle.blocked(throttle_key):
        return render(request, "login.html", user=None, error="Too many failed attempts. Try again in 15 minutes or reset your password.", next=next, email=email, status_code=429)
    user = authenticate(db, email, password)
    if user is None:
        security.login_throttle.failed(throttle_key)
        return render(request, "login.html", user=None, error="Incorrect email or password.", next=next, email=email, status_code=400)
    security.login_throttle.succeeded(throttle_key)
    memberships = memberships_for(db, user)
    login_user(request, user, memberships[0].organization_id if memberships else None)
    target = next if next.startswith("/") and not next.startswith("//") else "/app"
    return redirect(target)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return redirect("/")


# --- password reset -------------------------------------------------------


@router.get("/forgot")
def forgot_form(request: Request):
    return render(request, "forgot.html", user=None)


@router.post("/forgot", dependencies=[Depends(verify_csrf)])
def forgot(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is not None:
        token = security.sign_reset_token(user.id, user.password_hash)
        link = f"{get_settings().base_url}/reset/{token}"
        send_email(
            user.email,
            "Reset your SemanticOne password",
            f"Use this link within one hour to choose a new password:\n\n{link}\n\nIf you did not ask for this, ignore this email.",
        )
    flash(request, "If that email has an account, a reset link is on its way.", "info")
    return redirect("/login")


@router.get("/reset/{token}")
def reset_form(request: Request, token: str):
    if security.load_reset_token(token) is None:
        flash(request, "That reset link is invalid or expired.", "error")
        return redirect("/forgot")
    return render(request, "reset.html", user=None, token=token)


@router.post("/reset/{token}", dependencies=[Depends(verify_csrf)])
def reset(request: Request, token: str, password: str = Form(...), db: Session = Depends(get_db)):
    data = security.load_reset_token(token)
    user = db.get(User, data["uid"]) if data else None
    if user is None or user.password_hash[-16:] != data.get("ph"):
        flash(request, "That reset link is invalid or expired.", "error")
        return redirect("/forgot")
    problem = security.password_problem(password)
    if problem:
        return render(request, "reset.html", user=None, token=token, error=problem, status_code=400)
    user.password_hash = security.hash_password(password)
    db.commit()
    flash(request, "Password updated. Sign in with your new password.", "success")
    return redirect("/login")


# --- invitations ----------------------------------------------------------


@router.get("/invite/{token}")
def invite_landing(request: Request, token: str, user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    invite = _pending_invite(db, token)
    if invite is None:
        flash(request, "That invitation is invalid or was already used.", "error")
        return redirect("/login")
    if user is None:
        existing = db.scalar(select(User).where(User.email == invite.email.lower()))
        if existing is None:
            return redirect(f"/signup?invite={token}")
        return render(request, "invite.html", user=None, invite=invite, token=token, needs_login=True)
    return render(request, "invite.html", user=user, invite=invite, token=token, needs_login=False)


@router.post("/invite/{token}", dependencies=[Depends(verify_csrf)])
def invite_accept(request: Request, token: str, user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    invite = _pending_invite(db, token)
    if invite is None or user is None:
        return redirect("/login")
    if user.email != invite.email.lower():
        flash(request, f"This invitation was sent to {invite.email}. Sign in with that address to accept it.", "error")
        return redirect("/app")
    exists = db.scalar(
        select(Membership).where(Membership.user_id == user.id, Membership.organization_id == invite.organization_id)
    )
    if exists is None:
        db.add(Membership(user_id=user.id, organization_id=invite.organization_id, role=invite.role))
    invite.accepted_at = utcnow()
    db.commit()
    request.session["oid"] = invite.organization_id
    flash(request, f"You joined {invite.organization.name}.", "success")
    return redirect("/app")
