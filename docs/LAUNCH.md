# Launch guide: from this repo to paying customers

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
