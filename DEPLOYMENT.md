# RGNR8 — Deployment & Go-Live Runbook

The engines are built and tested; this is how you stand the platform up for UAT and, later, production. Nothing here needs the beta clients' QuickBooks credentials except the final "connect a real company" step.

## What runs where

RGNR8 is a modular monorepo with two runtimes that share cross-language JSON contracts:

- **TypeScript** — the accounting core (`@rgnr8/ledger-kernel`, `ingestion`, `reconciliation`, `subledger`, `statements`, `close`, `ledger-postgres`), QuickBooks migration + onboarding (`qbo-migrate`), connectors (`connectors`), and the forward-only SQL migration runner (`migrations`).
- **Python** — forecasting (`rgnr8-forecast`), the owner briefing + Today dashboard (`rgnr8-briefing`), the multi-tenant web app (`rgnr8-web`), the delivery runtime (`rgnr8-runtime`), and the operator layer (`rgnr8-ops`).

They meet at three contracts: `forecast-inputs/1` (ledger/onboarding → forecast), `financial-package/1` (sealed period package, fingerprint re-verified in both languages), and `ops-status/1` (connector health + close progress → the operator dashboard).

## Prerequisites

- Node 20+ and Python 3.11+.
- A PostgreSQL database (sqlite works for UAT/local; the SQL stores are DB-API 2.0 / standard-SQL and take a `placeholder` of `?` for sqlite or `%s` for psycopg).
- Secrets: a JWT signing key (HS256) for owner session tokens; later, SendGrid/FCM API keys for delivery and Plaid/Gusto + QBO OAuth for live data.

## 1. Create the schema

Two halves, both idempotent and forward-only.

**Ledger + financial-package tables (TypeScript):** run `LEDGER_MIGRATIONS` (and the financial-package migrations) through `@rgnr8/migrations` `runMigrations(db, LEDGER_MIGRATIONS_WITH_RLS, { appliedAt })`. Migrations are applied once, in order, checksum-drift-guarded.

**Python-owned tables:** one call creates web tenant-state, briefing subscriptions, and the fleet roster:

```python
from rgnr8_ops import bootstrap_python_schemas
bootstrap_python_schemas(conn, placeholder="%s")   # "?" for sqlite
```

## 2. Configure

Provide via environment/secret store:

- `RGNR8_JWT_SECRET` — HS256 signing key for owner session JWTs (rotate via your IdP; the web app verifies with `JwtAuthenticator`).
- Database DSN for the shared Postgres. **The role in this DSN must not be a
  superuser and must not have `BYPASSRLS`.** Postgres does not apply row-level
  security to either, and `FORCE ROW LEVEL SECURITY` does not change that — it only
  extends RLS to a table's *owner*. Connect as a superuser and every tenant-isolation
  policy below is defined, tested, and completely inert, with nothing in the app
  behaving differently. Owning the tables is fine; being a superuser is not. Managed
  Postgres (Render, RDS, Cloud SQL) hands you a non-superuser role by default —
  self-hosted setups are where this goes wrong.
  The app checks its own role at boot and logs `[security] warning: …` when it
  cannot enforce RLS. Set `RGNR8_REQUIRE_RLS=1` to make that fatal instead of a log
  line — recommended for any environment holding real client books.
- (When delivering) `SENDGRID_API_KEY` / FCM credentials for `rgnr8-briefing`'s HTTP transports.
- (When connecting data) Plaid/Gusto keys and the QBO OAuth token + realm id.

## 3. Serve the web app

The web app is framework-free; serve it through the WSGI adapter behind any WSGI server (gunicorn/uwsgi/waitress) terminating TLS at your load balancer:

```python
# rgnr8_web_entry.py
from rgnr8_web import wsgi_app
from your_bootstrap import build_app        # constructs WebApp with JwtAuthenticator + package reader + SqlTenantStore
application = wsgi_app(build_app())
```

```
gunicorn rgnr8_web_entry:application --workers 4 --bind 0.0.0.0:8080
```

Per-tenant isolation: every `/api/<tenant>/…` and `/t/<tenant>/…` route is tenant-scoped and a token for one tenant cannot reach another's data (enforced + tested). The Python-owned tables are behind Postgres RLS keyed on `tenant_id` (migrations 2, 4 and 5 — `ENABLE` + `FORCE ROW LEVEL SECURITY`), and the stores set the per-request `app.tenant_id` GUC so the policies engage. **This is only real if the connecting role cannot bypass RLS — see the DSN note in *Configure* above.**

### Health & readiness

- `GET /health` → `{"status":"ok"}` — liveness (no auth).
- `GET /ready` → `{"status":"ready","tenants":N,"packages":bool,"auth":"JwtAuthenticator","version":…}` — readiness for the load balancer / deploy check (no auth). Point your LB health check here.

## 4. Run the delivery runtime

`rgnr8-runtime`'s `DeliveryRuntime.tick(now)` is the weekly-briefing sender: forecast → validated briefing → deliver → persist `last_sent`. Drive it on a scheduler (cron/systemd timer/K8s CronJob) against the shared `SqlSubscriptionStore`. It's restart-safe (the delivery cursor is persisted) and idempotent (re-ticking the same fire window sends nothing new). Manage recipients with `SubscriptionManager` (add/list/pause/resume/remove); paused subscriptions stay stored but never fire.

## 5. Operate the fleet

`rgnr8-ops` `Fleet` provisions each beta client across the web surface, delivery runtime, and auth in one call, and — when constructed with a `SqlFleetStore` — persists the roster so it survives restarts (`Fleet.load(store, …)` rehydrates tenants, inputs, floor, schedule, and last-known status). `build_ops_report(fleet, now)` + `render_ops_html` draw the worst-first dashboard: cash status, floor, trough, breach timing, **Books** (connector health) and **Close** (close progress), and delivery state. Feed the Books/Close columns from the TS `opsConnectorStatus` / `opsCloseStatus` serializers via `Fleet.set_status_from_json`.

## 6. UAT

`python packages/prototype/uat.py` runs the whole operationalized stack against three demo businesses on a throwaway SQLite DB: bootstrap → onboard 3 → attach ops-status → deliver → **restart & rehydrate** → serve owners over WSGI with minted JWTs + `/ready` → render the dashboard. It asserts each step and exits non-zero on any failure — a good smoke test to wire into CI or run before a UAT session. `python packages/prototype/run.py` and `onboard.py` demonstrate the accounting + onboarding paths end to end.

## 7. Go live with a real company (QBO — planned Wed/Thu)

Everything above runs on fixtures. To connect a real QuickBooks company:

1. Supply a `fetch`-backed `QboHttpClient` + OAuth access token + realm id.
2. **Overlay (Stage 1):** `client.fetchOverlaySnapshot(asOf)` → `buildOverlayForecastInputs` → a `forecast-inputs/1` DTO. Provision with `Fleet.onboard_from_dto`. QBO stays the system of record; RGNR8 overlays the cash outlook.
3. **Migration (Stage 2):** `client.fetchExportViaGeneralLedger(start, end)` → `runMigrationOnboarding` (imports the GL into the RGNR8 ledger, verifies against QBO's reported trial balance, derives opening cash from the posted ledger). Provision the resulting DTO the same way.
4. Run the forecast over the client's history and feed predicted-vs-actual into `run_backtest` to calibrate the confidence tiers.

## Rollback

Schema changes are forward-only; roll back application code by redeploying the prior build. The ledger is append-only (corrections are reversals, never edits), and published financial packages are immutable — so a bad deploy never corrupts booked history. Disable delivery by pausing subscriptions (`SubscriptionManager.pause`) rather than deleting them, preserving cursors.
