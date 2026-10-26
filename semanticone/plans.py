"""Subscription plans and their limits. Prices live in Stripe; amounts here are for display."""

from __future__ import annotations

from dataclasses import dataclass

UNLIMITED = -1


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    price_label: str
    tagline: str
    max_models: int
    max_connections: int
    max_api_keys: int
    max_members: int
    monthly_queries: int
    max_rows: int
    warehouse_connections: bool
    features: tuple[str, ...]
    self_serve: bool = True
    monthly_price_cents: int = 0  # list price used when creating Stripe prices and estimating MRR

    def allows(self, limit: int, current: int) -> bool:
        return limit == UNLIMITED or current < limit


PLANS: dict[str, Plan] = {
    "free": Plan(
        key="free",
        name="Free",
        price_label="$0",
        tagline="Model and explore on sample data.",
        max_models=2,
        max_connections=1,
        max_api_keys=1,
        max_members=2,
        monthly_queries=1_000,
        max_rows=1_000,
        warehouse_connections=False,
        features=(
            "2 semantic models",
            "Hosted sample warehouse",
            "Validation, versioning and diffs",
            "REST API and MCP server",
            "1,000 queries / month",
        ),
    ),
    "pro": Plan(
        key="pro",
        name="Pro",
        price_label="$149/mo",
        tagline="For data teams putting governed metrics in front of AI.",
        max_models=25,
        max_connections=5,
        max_api_keys=10,
        max_members=10,
        monthly_queries=50_000,
        max_rows=50_000,
        warehouse_connections=True,
        features=(
            "25 semantic models",
            "5 warehouse connections",
            "Postgres, Snowflake, BigQuery, Databricks, MySQL, Redshift",
            "10 seats, 10 API keys",
            "50,000 queries / month",
        ),
        monthly_price_cents=14_900,
    ),
    "business": Plan(
        key="business",
        name="Business",
        price_label="$599/mo",
        tagline="Org-wide semantic layer with higher limits.",
        max_models=UNLIMITED,
        max_connections=25,
        max_api_keys=UNLIMITED,
        max_members=50,
        monthly_queries=500_000,
        max_rows=200_000,
        warehouse_connections=True,
        features=(
            "Unlimited semantic models",
            "25 warehouse connections",
            "50 seats, unlimited API keys",
            "500,000 queries / month",
            "Priority support",
        ),
        monthly_price_cents=59_900,
    ),
    "enterprise": Plan(
        key="enterprise",
        name="Enterprise",
        price_label="Custom",
        tagline="Dedicated deployment, SSO and SLAs.",
        max_models=UNLIMITED,
        max_connections=UNLIMITED,
        max_api_keys=UNLIMITED,
        max_members=UNLIMITED,
        monthly_queries=UNLIMITED,
        max_rows=1_000_000,
        warehouse_connections=True,
        features=(
            "Unlimited everything",
            "Single-tenant or VPC deployment",
            "SSO / SAML (roadmap)",
            "Uptime SLA and onboarding",
        ),
        self_serve=False,
    ),
}

DEFAULT_PLAN = "free"


def get_plan(key: str | None) -> Plan:
    return PLANS.get(key or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])


def fmt_limit(value: int) -> str:
    return "Unlimited" if value == UNLIMITED else f"{value:,}"
