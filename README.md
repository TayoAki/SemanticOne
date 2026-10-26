# SemanticOne

**The hosted semantic layer for the Apache Ossie standard.** Define business metrics once in vendor-neutral [Apache Ossie](https://github.com/apache/ossie) YAML, then serve the same governed numbers to dashboards, notebooks, apps and AI agents over a REST API and an MCP server.

SemanticOne is a commercial, multi-tenant SaaS: sign-up, workspaces and teams, Stripe subscriptions with plan limits, encrypted warehouse connections, usage metering and a production Docker image.

> Apache Ossie is a trademark of the Apache Software Foundation. SemanticOne is an independent product built on the open specification and is not affiliated with or endorsed by the ASF.

## What customers get

| Capability | Details |
| --- | --- |
| Model registry | Upload/paste/edit Ossie documents; JSON Schema plus name-resolution validation on every save; 0.1 → 0.2 auto-migration; version history with diffs, restore and download. |
| Correct-by-construction query engine | Each aggregate is evaluated from its own root dataset, joining only many-to-one, then combined on the requested dimensions, so there is no fan-out or chasm-trap double counting. Unsafe questions are refused with an explanation. Supports time grains, filters, metric filters (HAVING), window metrics, ratios across fact tables, and dimension-only queries. |
| Multi-warehouse SQL | Dialect-correct SQL via sqlglot for DuckDB, PostgreSQL, Redshift, Snowflake, BigQuery, Databricks, MySQL (and T-SQL for compile-only). Engine-specific `dialects` entries win, and portable `sql` / `ANSI_SQL` is transpiled. |
| AI agents (MCP) | Remote MCP server at `/mcp` (Streamable HTTP). Tools: `list_models`, `describe_model`, `query_metrics`, `compile_sql`, `validate_model`. Agents see descriptions, synonyms and `ai_context` instructions. |
| REST API | `/api/v1/...` with API keys: list/create/describe models, get/put documents (CI-friendly), validate, compile, query, usage. OpenAPI docs at `/api/docs`. |
| Explorer | Point-and-click metrics × dimensions with filters, charts, generated SQL and the join plan, plus "copy as API call". |
| Sample warehouse | Every workspace gets a sandboxed DuckDB e-commerce warehouse and a matching model, so new users see real numbers within a minute. |
| SaaS plumbing | Workspaces, invitations, admin/member roles, password reset, Stripe Checkout + Customer Portal + webhooks, plan limits (models, connections, seats, API keys, monthly queries, rows/query), usage log. |

## Quick start (local)

```bash
uv sync                      # Python 3.11+
uv run semanticone serve --reload
# open http://localhost:8000, sign up, open "Acme Commerce", then Explore
```

SQLite and a generated `./data` directory are used by default. Run the tests with `uv run pytest`. To include the PostgreSQL integration tests, set `TEST_POSTGRES_WAREHOUSE_URL` and `TEST_POSTGRES_APP_URL`; CI does this with a Postgres service.

### Production-like stack

```bash
docker compose up --build     # app + PostgreSQL on http://localhost:8000
```

## Deploying

The app is a single container (`Dockerfile`) plus PostgreSQL.

1. Provision PostgreSQL and set `DATABASE_URL`.
2. Set `ENVIRONMENT=production`, `APP_BASE_URL=https://your-domain`, and the variables in [`.env.example`](.env.example).
3. Mount a small persistent volume at `/data`.
4. Secrets: on a single instance, `SECRET_KEY` and `ENCRYPTION_KEY` are generated on first boot into `/data/instance_keys.json` (mode 0600, kept apart from the database). Back that file up, because losing it makes saved warehouse credentials unreadable. With several replicas, set both variables explicitly instead (`uv run semanticone generate-key`).
5. Migrations run automatically on start and are safe with multiple workers. `semanticone migrate` is also available as a release step.

**Railway:** `railway.json` is included (Dockerfile build, pre-deploy migration, `/healthz` health check). Create a project with a PostgreSQL service and this repo, attach a volume at `/data`, set `RAILWAY_RUN_UID=0` (Railway volumes are root-owned), then set the variables. **Anywhere else:** any container host (Fly.io, Render, ECS, Cloud Run, Kubernetes) works the same way.

### Operator console and Stripe

Set `ADMIN_EMAILS` to your email. Signing up with that address unlocks `/admin`:
- **Overview:** estimated MRR, usage and the latest leads.
- **Workspaces:** set plans for invoiced customers.
- **Leads:** requests from the public "Talk to sales" form (`/contact`).

**One-paste Stripe:** paste a Stripe secret key under Admin → Payments. SemanticOne then creates the Pro and Business prices, a webhook endpoint (with its signing secret) and a customer portal configuration, all stored encrypted.

Alternatively, configure Stripe with environment variables, which take precedence:

1. In Stripe, create two recurring prices (Pro and Business) and set `STRIPE_PRICE_PRO` / `STRIPE_PRICE_BUSINESS`.
2. Set `STRIPE_SECRET_KEY`.
3. Add a webhook endpoint `https://your-domain/webhooks/stripe` for `checkout.session.completed`, `customer.subscription.created|updated|deleted` and `invoice.payment_failed`, then set `STRIPE_WEBHOOK_SECRET`.
4. Enable the Customer Portal (plan switching, cancellation, invoices).

Without Stripe, upgrade buttons open the "Talk to sales" form; set plans for invoiced customers under Admin → Workspaces (or `semanticone set-plan <workspace-id> pro|business|enterprise`).

## Configuration

See [`.env.example`](.env.example). Key settings:

- `ALLOW_PRIVATE_NETWORKS` (default off) controls whether warehouse hosts that resolve to private or loopback addresses are allowed. Keep it off for the hosted multi-tenant service; turn it on for single-tenant/VPC installs.
- `SIGNUPS_ENABLED` turns open sign-up on or off; invitations always work.
- Plan limits and display prices live in `semanticone/plans.py`. Charged prices live in Stripe.
- Optional warehouse drivers: `uv sync --extra snowflake --extra bigquery --extra databricks --extra mysql`, or `docker build --build-arg EXTRAS="snowflake,bigquery,databricks,mysql" .`

## Architecture

```
semanticone/
  spec/        Ossie schema (vendored), YAML/JSON loader, 0.1→0.2 migration, semantic validation
  engine/      query request parsing, fan-out-safe SQL compiler, connectors, sample warehouse
  services.py  tenant-aware operations shared by UI, API and MCP (limits, metering, encryption)
  web/         server-rendered UI (Jinja2) + small vanilla JS for editor and explorer
  api.py       REST API v1          mcp.py   MCP server          billing.py   Stripe
  models.py    SQLAlchemy models    migrations/  Alembic          cli.py       operator CLI
```

Security:
- Passwords are hashed with scrypt.
- API keys are stored as SHA-256 hashes and shown once.
- Warehouse credentials are encrypted with Fernet.
- Every form has CSRF protection.
- Session cookies are secure under HTTPS.
- Warehouse hosts are re-checked against private address ranges on every use, to stop SSRF.
- Model expressions are restricted to scalar/aggregate SQL (no statements or subqueries).
- Filter values are always literals.
- The sample warehouse runs read-only with DuckDB file and network access disabled.

## Licensing

Proprietary (see `LICENSE`). The vendored Ossie JSON Schema and TPC-DS example are Apache-2.0 (see `NOTICE` and `third_party/apache-ossie/`). See [`docs/LAUNCH.md`](docs/LAUNCH.md) for the go-to-market checklist, trademark rules and roadmap.
