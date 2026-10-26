# Launch guide: from this repo to paying customers

## 0. Current deployment

Live at **https://semanticone.up.railway.app**: Railway project `semanticone`, service `web`, deploying the
`claude/busy-cannon-gjs04b` branch on every push.

Already in place:
- PostgreSQL service, wired in as `DATABASE_URL=${{Postgres.DATABASE_URL}}`.
- A 1 GB volume at `/data`, with `RAILWAY_RUN_UID=0` so the app can write to it.
- `ENVIRONMENT=production`, `APP_BASE_URL`, and the `/healthz` health check.
- Signing and encryption keys, generated on first boot in `/data/instance_keys.json`. They never left the server.

Verified on the live site:
- Sign-up, the sample model, explorer queries, the unsafe-query refusal, API keys, the REST API and MCP (including with the official MCP SDK client).
- HSTS and security headers.

How to sell today (no extra setup):
1. Sign up at the live URL with the email in `ADMIN_EMAILS`, then open `/admin/claim` and enter the operator code.
   - **Where to find the code:** Railway → `web` → Deployments → View logs (line "Operator console not claimed yet…"), or run `semanticone operator-code` in `railway ssh`.
   - **Why a code:** sign-up doesn't verify email ownership, so matching `ADMIN_EMAILS` alone grants nothing.
   - Once claimed, that account gets the **Admin** console at `/admin`.
2. Prospects use **Talk to sales**: the pricing page, the in-app billing page and `/contact`. Leads appear under Admin → Leads.
3. Invoice them however you like (Stripe Invoicing, PayPal, bank transfer), then set their plan under Admin → Workspaces.
4. For self-serve card payments, paste your Stripe secret key under Admin → Overview → Payments. The app creates the Pro and Business prices, the webhook and the customer portal itself. Try an `sk_test_` key first (card 4242 4242 4242 4242), then switch to `sk_live_`.

Recommended next (each needs your accounts or decisions):
- `SALES_EMAIL` plus `SMTP_*`, so lead alerts, password resets and invites are emailed. Until then leads wait in the Admin console, invite links are copied from the Team page, and password resets need SMTP.
- A custom domain (Railway → service → Settings → Networking), then update `APP_BASE_URL`. If Stripe is already connected, reconnect it so the webhook points at the new domain.
- Back up `/data/instance_keys.json`: `railway ssh` → `cat /data/instance_keys.json`, then store it in your password manager.
- A smoke-test workspace (`launch-smoke-test`, user `smoke-test-1793045881@example.com`, API key revoked) was created while verifying the deployment. Delete it if you like.

**Zero-downtime deploys.** A Railway service with a volume can't run two deployments at once, so each deploy has a few seconds of downtime. To remove it:
- Copy the two values from `/data/instance_keys.json` into the `SECRET_KEY` and `ENCRYPTION_KEY` service variables.
- Remove the volume and `RAILWAY_RUN_UID`.
- Optionally scale to 2 replicas.

The sample warehouse file is rebuilt automatically in each container.

## 1. Go live (about a day)

- [ ] Deploy (see README). Railway: new project → add PostgreSQL → deploy this repo → set variables → add a custom domain.
- [ ] Generate `SECRET_KEY` / `ENCRYPTION_KEY` with `semanticone generate-key` and back up `ENCRYPTION_KEY` in a password manager.
- [ ] Stripe: create Pro ($149/mo) and Business ($599/mo) prices, the webhook and the Customer Portal (README → Stripe). Test with Stripe test keys first: sign up, upgrade, cancel, and watch the plan badge change.
- [ ] Transactional email (Postmark, SES, Resend or similar) via `SMTP_*`, for password resets and invitations.
- [ ] Set `LEGAL_ENTITY`, `SALES_EMAIL`, `SUPPORT_EMAIL`.
- [ ] Uptime monitor on `/healthz`, plus database backups (managed Postgres point-in-time recovery).

## 2. Legal and trademark

- **Entity and terms.** Form a company before charging. The included Terms and Privacy pages are reasonable starting points, not legal advice: have a lawyer review them and prepare a DPA for business customers.
- **Apache Ossie trademark** (ASF policy):
  - Don't put "Apache" or "Ossie" in your product name, logo or domain, and don't imply endorsement.
  - Factual statements are fine: "built on the Apache Ossie open standard", "imports and exports Apache Ossie models".
  - Keep the footer acknowledgement: "Apache Ossie is a trademark of the ASF; not affiliated".
- **Open-source obligations** (already handled):
  - Keep `NOTICE` and `third_party/apache-ossie/` in the repo and the image.
  - psycopg is LGPL-3.0; that's fine for SaaS and for shipping the unmodified library in a container.
- **Spec stability.** The Ossie spec is a pre-release (`0.2.0.dev0`) inside the Apache Incubator. SemanticOne vendors a pinned copy of the schema and migrates older documents. When Ossie ships a release, update `semanticone/spec/schema/ossie-schema.json` and `SPEC_VERSION`, and add a migration step.

## 3. Positioning and pricing

**Who buys.** Data and analytics engineering teams (5–200 people) on Snowflake, BigQuery, Databricks or Postgres who are putting AI assistants and agents on top of company data and have been burned by inconsistent numbers.

**Pitch.** "Define a metric once, in an open standard, and every tool, including AI agents, gets the same correct answer. No lock-in: your models are plain Apache Ossie YAML."

**Differentiators to lead with:**
1. *Correctness.* Answers can't double count, and the engine refuses unsafe questions instead of guessing. Demo it live: "support tickets by product category" gets refused with an explanation.
2. *AI-native.* An MCP server with metric descriptions, synonyms and instructions; set up in two minutes.
3. *Vendor-neutral.* Works across warehouses, and models stay in an open format. Snowflake semantic views and Databricks metric views each lock you into one warehouse; the dbt Semantic Layer requires dbt Cloud.

**Pricing** (edit `semanticone/plans.py` and Stripe together):
- Free lets people evaluate on the sample warehouse.
- Pro ($149/mo) is the first paid step, gated on connecting a real warehouse.
- Business ($599/mo) is for org-wide use.
- Enterprise (custom, $20k+/yr) adds single-tenant deployment, SSO and an SLA, and is invoiced manually with `semanticone set-plan`.

## 4. First customers

- Recruit 5 design partners from the Ossie community (GitHub Discussions, the `apache-ossie` Slack), dbt and Snowflake communities, and LinkedIn data-leader networks. Offer 3 months of Pro free in exchange for weekly feedback.
- Content that sells this product:
  - "Why your AI agent's revenue number is wrong (fan-out explained)" with the explorer demo.
  - "Ossie in 10 minutes".
  - "Connect Claude to governed metrics".
- List the MCP server in MCP directories once there's a public demo workspace.
- Contribute upstream to Apache Ossie (converters, compliance tests). Committer status builds credibility and gives you a say in the spec.

## 5. Roadmap (not built yet, in rough order of what buyers ask for)

1. **SSO / SAML and SCIM** (Enterprise gate) and an **audit log** of model changes and queries.
2. **Access policies**: per-model/per-metric permissions and row-level filters per user or API key.
3. **Result caching** and per-minute rate limits per API key.
4. **In-app import/export** with the official `apache/ossie-converters` (dbt, Snowflake, Cube, Tableau, …). Today users convert with the CLIs and upload.
5. **BI connectivity**: a Postgres wire-protocol / JDBC endpoint so Tableau, Power BI and Excel can query metrics directly.
6. **Ossie query-interface layers**: track the spec's Layer 2 (SQL with measures) and Layer 3 (wide table) and the official compliance suite as they finalize.
7. **Scheduled reports and alerts**, Slack integration.
8. **Compliance**: SOC 2 Type I, then Type II (Vanta or Drata), plus a penetration test before larger deals.
