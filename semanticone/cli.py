"""Operator CLI: `semanticone serve | migrate | generate-key | set-plan | list-workspaces`."""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import func, select


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "semanticone.main:app_factory",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        proxy_headers=True,
        forwarded_allow_ips="*",
        reload=args.reload,
    )
    return 0


def _migrate(_args: argparse.Namespace) -> int:
    from .db import get_engine
    from .migrate import upgrade_database

    upgrade_database(get_engine())
    print("Database is up to date.")
    return 0


def _generate_key(_args: argparse.Namespace) -> int:
    from cryptography.fernet import Fernet
    import secrets

    print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
    print(f"ENCRYPTION_KEY={Fernet.generate_key().decode()}")
    return 0


def _set_plan(args: argparse.Namespace) -> int:
    from .db import session_scope
    from .models import Organization
    from .plans import PLANS

    if args.plan not in PLANS:
        print(f"Unknown plan {args.plan!r}; choose from {', '.join(PLANS)}", file=sys.stderr)
        return 2
    with session_scope() as db:
        org = db.scalar(select(Organization).where(Organization.slug == args.workspace))
        if org is None:
            print(f"No workspace with id {args.workspace!r}", file=sys.stderr)
            return 1
        org.plan = args.plan
        # Manually invoiced plans are not tied to a Stripe subscription.
        org.subscription_status = None if args.plan in ("free", "enterprise") else "active"
        print(f"{org.slug}: plan set to {args.plan}")
    return 0


def _list_workspaces(_args: argparse.Namespace) -> int:
    from .db import session_scope
    from .models import Membership, Organization

    with session_scope() as db:
        rows = db.execute(
            select(Organization.slug, Organization.name, Organization.plan, func.count(Membership.id))
            .join(Membership, Membership.organization_id == Organization.id, isouter=True)
            .group_by(Organization.id)
            .order_by(Organization.id)
        ).all()
        for slug, name, plan, members in rows:
            print(f"{slug:30} {plan:10} {members:3} members  {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="semanticone")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the web server")
    serve.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    serve.add_argument("--workers", type=int, default=int(os.environ.get("WEB_CONCURRENCY", "1")))
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=_serve)

    sub.add_parser("migrate", help="Apply database migrations").set_defaults(func=_migrate)
    sub.add_parser("generate-key", help="Print fresh SECRET_KEY and ENCRYPTION_KEY values").set_defaults(func=_generate_key)

    set_plan = sub.add_parser("set-plan", help="Set a workspace's plan (for invoiced/enterprise customers)")
    set_plan.add_argument("workspace", help="Workspace id (slug)")
    set_plan.add_argument("plan")
    set_plan.set_defaults(func=_set_plan)

    sub.add_parser("list-workspaces", help="List workspaces and plans").set_defaults(func=_list_workspaces)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
