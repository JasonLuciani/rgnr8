# rgnr8-web

The web app shell — a **framework-free, multi-tenant HTTP surface** for the owner experience. It serves the interactive Today dashboard, a JSON API, the text briefing, and the ask-your-CFO endpoint, behind bearer-token auth, with every route tenant-scoped.

Python, depends only on `rgnr8-forecast` + `rgnr8-briefing` (no web framework). 47 tests (pure-handler + live-socket + persistence), `mypy --strict` clean.

## Design

The core is a **pure function** — `WebApp.handle(Request) -> Response` — with no sockets, so the entire routing/auth surface is unit-testable without a running server. `server.py` wraps it in the stdlib `http.server` for a runnable process. A tenant is registered with its `ForecastInputs`, config, and a bearer token; the forecast is computed once per tenant and cached.

## Routes

| Method | Route | Auth | Returns |
|---|---|---|---|
| GET | `/health` | none | `{"status":"ok"}` |
| GET | `/t/<tenant>` | bearer | the interactive Today dashboard (HTML) |
| GET | `/api/<tenant>/today` | bearer | JSON summary (status, headline, action, cash, floor, trough, breach, 13 weeks, confidence, version) |
| GET | `/api/<tenant>/briefing.txt` | bearer | the owner briefing (text) |
| POST | `/api/<tenant>/ask` | bearer | `{text}` → constrained ask-your-CFO answer |
| POST | `/api/<tenant>/assumptions` | bearer | `{minimum_cash?, payment_override?}` → **recomputes** the forecast, returns the new summary |
| GET / POST | `/api/<tenant>/decisions` | bearer | list / append the decision log (records resolved actions + assumption changes) |
| GET | `/api/<tenant>/packages` | bearer | list the period keys of published financial packages |
| GET | `/api/<tenant>/packages/<period>` | bearer | the sealed financial package (JSON), **re-verified**; `409` if integrity fails |
| GET | `/t/<tenant>/packages/<period>` | bearer | the published package rendered as HTML (statements + TB + verified badge) |

## The write path (actionable surface)

The dashboard isn't read-only. `POST /assumptions` lets an owner change the minimum-cash floor or a customer's payment timing; the tenant's cached forecast is invalidated and **recomputed on the spot**, so the returned summary (and the next dashboard load) reflect the change. Every change and any explicitly resolved action is appended to a per-tenant **decision log** — the blueprint's retention loop ("the owner resolves, corrects, or records a decision; the system learns and measures the result"). Writes are tenant-isolated like every other route (tests cover a 403 across tenants).

## Auth (static tokens or JWT sessions)

Auth sits behind an `Authenticator` seam (`tenant_for(headers) -> tenant_id | None`). The default is a static bearer-token→tenant map (dev/tests). Production passes a **`JwtAuthenticator`**: the client presents a signed **HS256 session JWT**, verified without a lookup table. The dependency-free verifier (`sign_jwt`/`verify_jwt`) pins the algorithm to HS256 and **rejects `alg: none` and any algorithm mismatch** (the classic JWT-confusion attack), compares signatures in constant time, and checks `exp`/`nbf` against an **injected clock** (deterministic, testable). The tenant comes from a configurable claim; the app still requires that tenant to be registered, so a validly-signed token for an unknown tenant reaches nothing (`404`).

Regardless of authenticator: a missing/invalid token is `401`; a token reaching a **different** tenant is `403`; unknown tenant/route is `404`. Tenant isolation is enforced on every route and covered by tests (including a JWT for tenant A being blocked from tenant B). The `today` JSON re-runs the unsupported-number validator and returns `500` rather than serving an unbacked number.

## Durability & provisioning (the persistence seam)

Mutable owner state (the overrides and the decision log) is held behind a small `TenantStore` protocol — `load(tenant_id)` / `save(tenant_id, state)` — the same seam pattern as the kernel's `LedgerStore` and the connectors' `HttpClient`. Three implementations ship:

- **`InMemoryTenantStore`** (default) — state lives in-process, lost on exit.
- **`JsonFileTenantStore`** — one JSON file holds every tenant's state, written atomically (temp file + `replace`) so a crash mid-write can't corrupt it. This makes the write path **survive a restart** with no database; a test provisions a tenant, changes an assumption, then builds a *fresh* `WebApp` against the same file and confirms the floor and decisions rehydrate.
- **`SqlTenantStore`** — the same state as one JSON document per tenant in a SQL table, upserted with `INSERT … ON CONFLICT` over any PEP-249 (DB-API 2.0) connection. Tested with stdlib `sqlite3` (survives a fresh connection + `WebApp`; upsert keeps one row per tenant); production passes a `psycopg`/Postgres connection with `placeholder="%s"` — the same Postgres the ledger uses. No driver dependency is baked into the package.

Tenants are the immutable *definition* (forecast inputs + name, token, configured floor). A `TenantDef.from_dto(...)` builds one directly from the `forecast-inputs/1` DTO the TS `@rgnr8/forecast-inputs` side emits (dict or JSON text), and `WebApp.from_defs([...])` registers a batch — so the surface loads real tenants from the emitted contract rather than hand-built fixtures.

## The published financial package (cross-language, verified)

When a period closes, the **TypeScript** accounting stack (`@rgnr8/close`) seals its statements + trial balance + QBO reconciliation into a SHA-256-fingerprinted package and persists it to the `financial_package` SQL table. The web app reads those rows straight from that table via `FinancialPackageReader` and **independently re-verifies the fingerprint in Python** — `canonicalize` here is byte-identical to the TS one (recursively key-sorted, compact JSON, non-ASCII raw), so the same hash is reproduced from the stored bytes. A test embeds a real TS-emitted package as a fixture and asserts the Python verifier reproduces its fingerprint exactly; a DB-edited row is rejected as `409`/`PackageIntegrityError` rather than served as trustworthy. This is the same TS↔Python integrity boundary as `forecast-inputs/1`, now for the published record an owner downloads.

Pass a reader to enable the routes: `WebApp(store=..., packages=FinancialPackageReader(conn))`.

## Run it

```bash
python -m rgnr8_web 8080          # serves demo tenant 'bright' (bearer 'demo-token')
curl -H "Authorization: Bearer demo-token" http://127.0.0.1:8080/api/bright/today
# open http://127.0.0.1:8080/t/bright with the same header for the dashboard
python -m pytest                  # 47 tests
```

## Layout

```
src/rgnr8_web/
  app.py      Request/Response, WebApp, routing, bearer auth, tenant isolation, handlers
  server.py   http.server wrapper + serve() + runnable demo
  store.py    TenantStore protocol + in-memory/JSON-file impls, TenantState, TenantDef (DTO loader)
  sql_store.py SqlTenantStore — DB-API 2.0 backed (sqlite3 in tests, psycopg in prod)
  auth.py     Authenticator seam: static token map + HS256 JWT sessions (sign/verify)
  financial_package.py  read + re-verify the sealed package from the shared SQL table
  __main__.py python -m rgnr8_web
tests/        pure-handler + live-socket + persistence + JWT + package tests
```

## What's next / limits

- JWT session auth is built (`JwtAuthenticator`); wiring it to a real identity provider (token issuance/refresh, user↔tenant mapping) is the remaining integration. Tenant *definitions* still need loading from the DB / emitted `forecast-inputs/1` DTOs rather than registered by hand.
- Front it with TLS + a real WSGI/ASGI server for production; add rate limiting and per-tenant RLS at the data layer (the ledger already ships RLS DDL; the web-state table needs the same).
