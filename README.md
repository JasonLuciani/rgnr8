# RGNR8 Financial OS — codebase

Owner-first financial operating platform for small U.S. service businesses. Core promise: keep the financial record current and answer "Where's my cash?" with a verified 13-week cash outlook and a weekly owner briefing.

This repo is a monorepo. Per the build plan, the accounting **kernel** is implemented first — its invariants (integer money, append-only posting, provenance, idempotency) live in the data model from commit one so that everything built on top is ledger-compatible.

## Run the prototype

One command drives the **whole loop across both languages** on a demo business and writes the owner-facing pages to `packages/prototype/out/`:

```bash
cd packages/prototype && python run.py
```

QBO CSV → ledger → close → **sealed package** (TypeScript) → **re-verified in Python** → forecast → briefing → **Today dashboard** → served over **JWT-authed web** → **delivered via HTTP email transport**. See [`packages/prototype`](packages/prototype).

## Packages

| Package | Status | Purpose |
|---|---|---|
| [`packages/ledger-kernel`](packages/ledger-kernel) | ✅ built, 31 tests green | Money primitives + balanced-by-construction, append-only double-entry posting engine, periods, trial balance. Zero runtime deps; persistence behind an async `LedgerStore` interface. |
| [`packages/ledger-postgres`](packages/ledger-postgres) | ✅ built, 10 tests green | PostgreSQL `LedgerStore` adapter: atomic gap-free per-tenant sequencing, idempotency, exact NUMERIC money, tenant isolation + RLS. Schema ships as versioned `LEDGER_MIGRATIONS` run through `@rgnr8/migrations`. Tested via pg-mem. |
| [`packages/forecast`](packages/forecast) | ✅ built, 61 tests green (Python) | Direct-method 13-week cash forecast: deterministic, explainable, versioned. Payment-timing prediction, treatment policies, base/downside/upside scenarios, cash trough + minimum-cash breach, owner headline + recommended action. Plus a **backtesting harness** (`run_backtest`) that scores past published forecasts vs. actuals by horizon — MAPE/bias/RMSE per week-ahead (deterministic integer math) + breach precision/recall + accuracy "shelf life". `mypy --strict` clean, ~97% coverage. |
| [`packages/briefing`](packages/briefing) | ✅ built, 48 tests green (Python) | Weekly owner briefing over the forecast: status, evidence-backed facts + drivers, **unsupported-number validator**, ask-your-CFO answerer, text/HTML render + interactive **owner "Today" dashboard** (`render_today_html`), **variance vs. last published**, delivery envelopes, email/push providers over a transport seam — including **real HTTP-backed transports** (`HttpEmailTransport` SendGrid / `HttpPushTransport` FCM over an injected `HttpClient`, fully fake-tested) — and a **deterministic weekly scheduler** (`is_due`/`run_due`, timezone-correct, catch-up). Depends on `forecast`. `mypy --strict` clean. |
| [`packages/ops`](packages/ops) | ✅ built, 5 tests green (Python) | The **operator layer** for running beta clients: `Fleet.onboard` provisions a client consistently across the web surface, delivery runtime, and JWT auth in one call (`mint_token` issues session JWTs); `build_ops_report`/`render_ops_html` give a **fleet dashboard** — every client's cash status, floor, trough, breach timing, and briefing delivery state on one worst-first page. Depends on `forecast` + `briefing` + `web` + `runtime`. `mypy --strict` clean. |
| [`packages/runtime`](packages/runtime) | ✅ built, 9 tests green (Python) | The **delivery runtime**: a long-running process that ticks the weekly-briefing scheduler on a real clock, building each due tenant's validated briefing (forecast → briefing → validate → deliver) and persisting subscription `last_sent` so delivery is idempotent across restarts. `SubscriptionStore` (in-memory / SQL) + `TenantSource` (DTO-loadable) + injected-clock `tick`/`serve`. An end-to-end test drives it through the **real HTTP email transport** (fake client) to a SendGrid POST. Depends on `forecast` + `briefing`. `mypy --strict` clean. |
| [`packages/web`](packages/web) | ✅ built, 47 tests green (Python) | Framework-free multi-tenant HTTP shell serving the Today dashboard, JSON API, briefing, and ask endpoint behind an auth seam (static tokens or **HS256 JWT sessions** — `JwtAuthenticator`, alg-confusion/expiry guarded) — plus a **durable write path** (change minimum-cash / payment-timing assumptions → forecast **recomputes** on the spot; per-tenant **decision log**) behind a `TenantStore` seam: in-memory, atomic JSON-file, or **SQL-backed** (`SqlTenantStore` over DB-API 2.0 — sqlite3 in tests, Postgres in prod) — all survive restart. Tenants load from the `forecast-inputs/1` DTOs the TS side emits. Also **surfaces the published financial package** (sealed by the TS close stack) — reads it from the shared SQL table and **re-verifies the SHA-256 fingerprint in Python** (byte-identical canonicalization), serving it as JSON/HTML or `409` if integrity fails. Pure testable handler + stdlib server. Depends on `forecast` + `briefing`. `mypy --strict` clean. |
| [`packages/ingestion`](packages/ingestion) | ✅ built, 10 tests green (TS) | Raw bank/card/payroll payloads → canonical transactions: append-only archive, provider adapters (Plaid-like, Gusto-like), idempotent dedupe, pending→posted, internal-transfer detection, classification, and mapping to balanced kernel journals. Depends on `ledger-kernel`. |
| [`packages/reconciliation`](packages/reconciliation) | ✅ built, 9 tests green (TS) | Tie a bank/card statement to ingested book activity: match (external id, then amount+date), classify differences (timing vs missing-source vs error), compute book-vs-statement difference, **clean-for-publish gate**, reviewer sign-off. Depends on `ledger-kernel` + `ingestion`. |
| [`packages/forecast-inputs`](packages/forecast-inputs) | ✅ built, 10 tests green (TS) | Emits the Python forecast's `ForecastInputs` (JSON contract `forecast-inputs/1`) from reconciled data: opening cash **gated on a clean reconciliation**, recurring-flow detection, and merged AR/AP subledger parts. Round-trips with `rgnr8_forecast.io`. Cross-language demos (emit.ts / emit_with_ar.ts → consume.py). |
| [`packages/subledger`](packages/subledger) | ✅ built, 19 tests green (TS) | AR/AP subledgers: invoices/bills, partial payments, aging, **control-account reconciliation**, customer payment histories, forecast-input emission, **cash auto-application**, and **GL posting** (events → balanced kernel journals; control ties to subledger). Depends on `ledger-kernel`. |
| [`packages/statements`](packages/statements) | ✅ built, 4 tests green (TS) | Income statement + balance sheet from the ledger; balance sheet **balances by construction**. Depends on `ledger-kernel`. |
| [`packages/close`](packages/close) | ✅ built, 29 tests green (TS) | Month-end close checklist that **gates the kernel period lock** on clean reconciliations, control accounts, and a balanced trial balance; a closed period rejects further postings. Plus the **immutable financial package**: `buildFinancialPackage`/`verifyFinancialPackage` freeze a closed period's statements + TB + QBO reconciliation into a SHA-256-fingerprinted, tamper-evident, ledger-reproducible artifact — and a **`FinancialPackageStore`** (in-memory / SQL) that persists it as a published record, verified on read and immutable once sealed. Plus a **close calendar** (`buildCloseCalendar`/`closeCalendarStatus`): sequences the month-end work with owners + business-day due dates (weekends/holidays skipped) and dependency-aware blocked/overdue/due-today states, with an HTML render. Depends on `ledger-kernel`. |
| [`packages/migrations`](packages/migrations) | ✅ built, 7 tests green (TS) | Forward-only SQL schema migrations over a minimal `SqlExecutor` seam (pg-mem-tested): ordered, idempotent, transactional, with **checksum drift detection** (an edited applied migration throws). Zero runtime deps. |
| [`packages/prototype`](packages/prototype) | ✅ runnable demo | **End-to-end harness**: `python run.py` drives QBO-CSV import → ledger → close → sealed package (TS), then re-verify → forecast → briefing → Today dashboard → JWT-authed web → HTTP email delivery (Python), emitting owner-facing HTML to `out/`. Not a unit — it wires the other packages into one runnable loop. |
| [`packages/qbo-migrate`](packages/qbo-migrate) | ✅ built, 28 tests green (TS) | **QuickBooks migration + parallel-close compare** (Phase 2): **CSV adapters** *and* a **QuickBooks Online API adapter** (`QboApiClient` over an injected HTTP seam — accounts, reported trial balance, and a **full-history GeneralLedger import** that reconstructs balanced entries across all transaction types → normalized `QboExport`; going live = OAuth token + realm id) import a QBO export into the ledger (type mapping, balanced posting, bad-row issues, idempotent), then diff RGNR8 against QBO — account-by-account trial balance (`compareTrialBalances`) **and** statement-level P&L/balance-sheet totals (`compareStatements`), to the penny, with HTML renders. The wedge to system-of-record. Depends on `ledger-kernel` + `statements`. |
| [`packages/connectors`](packages/connectors) | ✅ built, 22 tests green (TS) | Live incremental bank/payroll sync over a **pluggable HTTP transport**: Plaid `/transactions/sync` (cursor pagination) + Gusto payroll runs, a runner handling cursor advance, **token health** (expired/revoked), and **rate-limit retry/backoff**. Production `FetchHttp` client, **webhook-driven sync** (`handlePlaidWebhook`), a **connection-health dashboard** (`buildHealthReport`/`renderHealthHtml`), and a **periodic `SyncRuntime`** (due-based ticking, error backoff, skips reconnect-needed connections) + `serveSync` loop. Going live = real credentials. Depends on `ingestion`. |

## The loop, end to end

live bank/payroll (`connectors` — webhook + periodic `SyncRuntime`) → raw feed → `ingestion` (canonical) → `reconciliation` (clean-for-publish gate) + `subledger` (AR/AP: cash auto-application, control rec, GL posting) → `forecast-inputs` (gated DTO) → **`rgnr8-forecast`** (13-week forecast) → **`rgnr8-briefing`** (validated briefing + Today dashboard + delivery) → **`rgnr8-web`** (serves it, multi-tenant + auth) + **`rgnr8-runtime`** (ticks the scheduler and delivers the weekly briefing). Accounting stack over the same ledger: `ledger-kernel`/`ledger-postgres` → `statements` → `close`. Cross-language demo in `packages/forecast-inputs/examples`; the web surface via `python -m rgnr8_web`.

## Remaining for a running pilot

1. **Connector transport → production** — the sync/health/retry state machine, `FetchHttp` client, webhook-driven sync, and a connection-health dashboard are built (`packages/connectors`); going live needs real credentials, a periodic scheduler for non-webhook connections, OAuth token-refresh hooks, and statement-import adapters (OFX/CSV).
2. **Delivery runtime** — providers (email/push over a transport seam) and a deterministic scheduler are built (`packages/briefing`); remaining is a long-running process that ticks `run_due` on a real clock with live transports + a subscription store.
3. **Production web** — the write path is built and **durable** (behind a `TenantStore` seam; JSON-file *and* **SQL/Postgres** (`SqlTenantStore`) impls survive restart; tenants load from emitted `forecast-inputs/1` DTOs); still needs real sessions/JWT + identity provider, TLS + ASGI, per-tenant RLS.
4. **QBO migration/compare** (Phase 2) — CSV adapters, ledger import, and parallel-close diff (trial-balance **and** statement-level) are built (`packages/qbo-migrate`); still needs IIF/QBXML/API adapters and GL-detail CSV support. Versioned DB migrations ✅ (`packages/migrations`) with the **ledger schema now registered as `LEDGER_MIGRATIONS`**; immutable per-period financial package ✅ **and persisted** via `FinancialPackageStore` (`packages/close`); still to route the web-state tables through the runner and surface the published package. Forecast backtesting harness (`compute_variance`).

## Workspace

TypeScript packages use npm workspaces (`npm install` at root, `npm run build --workspaces`, per-package `npm test`). The `forecast` package is Python (`cd packages/forecast && python -m pytest`).

## Stack (from the blueprint)

Modular monolith + async workers · Node/TypeScript application services · Python forecasting · PostgreSQL · object storage for raw payloads · managed queues. The posting engine is the strongest early hard module boundary: a strict code-and-DB boundary now, a separate service optional later.

## Getting started

```bash
cd packages/ledger-kernel
npm install
npm test
node --import tsx examples/demo.ts
```

See the project docs (RGNR8 Blueprint v0.4 tightened, and the MVP Build & Execution Plan) for the full four-phase sequence.
