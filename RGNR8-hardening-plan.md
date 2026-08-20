# RGNR8 — Hardening Plan (ticket-level)

_Date: 2026-08-20. Turns the recovery sequence in `RGNR8-review-verification.md` into tracked tickets. Each ticket lists the finding(s) it closes, a fix outline, files touched, a concrete acceptance gate, dependencies, and a rough estimate (S ≤1d · M 2–3d · L 4–5d · XL >1wk). Finding IDs (D#, S#, P#, A#) refer to the verification scorecard._

## How to read this

Do the phases in order; within a phase, tickets can mostly run in parallel except where **Depends** says otherwise. Each ticket is "done" only when its **Acceptance** gate passes in CI (not just locally). No net-new feature work until Phase 0–1 are green.

| Phase | Theme | Tickets | Exit gate |
|---|---|---|---|
| 0 | Make it boot as one system | H0-1 … H0-10 | `docker compose up` serves an authenticated owner request end-to-end, ledger wired, and CI builds+boots the image |
| 1 | Security & isolation (pre-tenant-data) | H1-1 … H1-8 | No shared/absent ledger auth; secrets fail-closed; web-DB RLS real; scoped keys |
| 2 | Truth-in-UI & data lifecycle | H2-1 … H2-8 | No invalid statement can publish; PII encrypted; "erase" honest; audit evidence protected |
| 3 | Accounting controls to SoR grade | H3-1 … H3-8 | Atomic go-live/cutover, governed schema, durable close, correct cash-flow |
| 4 | Product narrowing & re-verification | H4-1 … H4-3 | Narrow v1 scope shipped; claims audited; independent re-verify passes |

Rough total: ~36 tickets. Phase 0 ≈ 2–3 wks, Phase 1 ≈ 2 wks, Phase 2 ≈ 2–3 wks, Phase 3 ≈ 3–4 wks (one engineer; parallelizes with more).

---

## Phase 0 — Make it real and safe to boot

**H0-1 · Fix the production image package/dependency set** — Closes D1 · P0 · Est M
The image copies 5 packages and installs only gunicorn+psycopg, but `entry:application` imports alerts, obs, ar, billing, categorize, qbo, reports, scenario, ocr, copilot and needs reportlab/openpyxl/cryptography.
Fix: copy every `packages/*/src` the import graph needs (or all of them) and install `requirements.txt`; update `deploy/_pathsetup.py` to match.
Files: `deploy/Dockerfile`, `deploy/_pathsetup.py`, `requirements.txt`.
Acceptance: `docker build` succeeds and `python -c "import entry"` inside the image succeeds.
Depends: none.

**H0-2 · Ledger service image + boot in the topology** — Closes D2 · P0 · Est L
The Node ledger service is never containerized or started by the stack.
Fix: add a Node build image for `@rgnr8/ledger-service` (build to `dist/`, `node dist/bin.js`); add it as a compose service with a healthcheck; web `depends_on` it.
Files: `deploy/Dockerfile.ledger` (new), `deploy/docker-compose.yml`, `deploy/Procfile`.
Acceptance: `docker compose up` starts the ledger and it answers its health endpoint before web accepts traffic.
Depends: none.

**H0-3 · Run the TypeScript ledger migrations in the release step** — Closes D2 (+ sets up A9) · P0 · Est M
`migrate.py` runs Python migrations only; TS migrations are referenced in prose only.
Fix: add a node migration step invoking `@rgnr8/migrations runMigrations` for ledger/subledger/financial-package; sequence it in the release/migrate phase alongside Python.
Files: `deploy/migrate.py` (or new `deploy/migrate_ledger.mjs`), `deploy/Procfile` (release), compose migrate service.
Acceptance: against a fresh DB, both Python and TS migrations apply and are idempotent on re-run; CI asserts.
Depends: H0-2.

**H0-4 · Wire the ledger client into the production composition** — Closes D2 · P0 · Est S
Production (`entry.py`/`app_factory`) never calls `set_ledger`; only `run_local.py` does.
Fix: build `LedgerClient` from `RGNR8_LEDGER_URL`/token and `set_ledger` in the composition root.
Files: `packages/ops/src/rgnr8_ops/app_factory.py`, `deploy/entry.py`.
Acceptance: a booted web app posts and reads a journal entry against the running ledger in a smoke test.
Depends: H0-2, H0-8.

**H0-5 · Working browser login in the shipped composition** — Closes D4 · P0 · Est M
`app_factory` never passes `session_secret`/credentials/auth_service; hs256 → `_sso_mode`=True → `POST /login` returns 400; no IdP flow wired.
Fix: choose the login model (self-issued session vs real SSO) and wire it in one place; document the env for each.
Files: `packages/ops/src/rgnr8_ops/app_factory.py`, `packages/web` login/session code.
Acceptance: a browser E2E logs in and reaches an owner dashboard page.
Depends: H0-8.

**H0-6 · Wire the real email transport** — Closes D3 · P0 · Est S
`worker.py` returns `RecordingDeliverer()` even with a key set.
Fix: construct `ProviderDeliverer(HttpEmailTransport(...))` when `sendgrid_api_key` present; keep the recorder only for explicit dry-run.
Files: `deploy/worker.py`.
Acceptance: unit test with a mock HTTP client asserts a send is attempted when a key is set; recorder used otherwise.
Depends: none.

**H0-7 · Persist onboarding / go-live state** — Closes D5 · P1 · Est M
`OnboardingRegistry` holds dicts; TS uses `InMemoryGoLiveQueue`; both lost on restart.
Fix: add `SqlOnboardingRegistry` (and a durable go-live job store) behind the existing interface.
Files: `packages/ops/src/rgnr8_ops/onboarding.py`, `operator_app.py`, ledger-kernel go-live queue.
Acceptance: go-live request/cutover state survives a process restart (test).
Depends: none.

**H0-8 · Collapse to one production composition** — Closes D6 · P1 · Est M
`entry.py` and `render_app.py` disagree on auth/durability/tenant/ledger/qbo; `render_app.py` would crash inside the image.
Fix: make one entrypoint authoritative (durable + RBAC + login + ledger + qbo + ask, env-gated); delete or reduce `render_app.py` to a thin alias.
Files: `deploy/entry.py`, `deploy/render_app.py`, `packages/ops/app_factory.py`.
Acceptance: exactly one prod WSGI path; a config matrix test shows each capability toggles from env.
Depends: none (enables H0-4, H0-5).

**H0-9 · CI gate: build + boot the Docker image** — Closes D7 · P0 · Est M
CI never builds/boots the image, so D1/D2/D6 shipped undetected.
Fix: add a CI job that `docker compose build`, brings the stack up, waits for `/ready`, runs a deploy smoke test (login + one owner request + one ledger read); switch `npm install`→`npm ci`.
Files: `.github/workflows/ci.yml`, `deploy/smoke_test.py` (new).
Acceptance: CI fails if the image can't boot or the smoke test fails.
Depends: H0-1…H0-6, H0-8.

**H0-10 · CI gate: migrations + restore drill + dependency scan** — Closes D7 · P1 · Est M
Fix: CI applies Python+TS migrations to a fresh DB, runs `deploy/backup.sh`→`restore.sh` round-trip, and adds a dependency/SBOM scan.
Files: `.github/workflows/ci.yml`.
Acceptance: CI fails on migration error, failed restore, or a flagged high-severity dependency.
Depends: H0-3.

---

## Phase 1 — Security & isolation (before any real tenant data)

**H1-1 · Per-tenant authorization + mandatory auth at the ledger boundary** — Closes S1 · P0 · Est L
One optional shared bearer; tenant taken from the URL; auth off entirely when the token is empty.
Fix: bind caller identity to tenant (per-tenant service credentials or a signed caller assertion the ledger verifies against the path); refuse to start without auth configured.
Files: `packages/ledger-service/src/handlers.ts`, `bin.ts`, `packages/web` ledger_client, composition env.
Acceptance: a credential scoped to tenant A is rejected on a tenant-B path; empty/absent auth → service refuses to start; tests.
Depends: H0-2, H0-4.

**H1-2 · Ledger on a private network only** — Closes S1 · P0 · Est S
Fix: ensure the ledger service is not publicly routable in any manifest; web reaches it over an internal network.
Files: `deploy/docker-compose.yml`, deploy manifests, `PROVISIONING-RUNBOOK.md`.
Acceptance: ledger has no public port mapping; documented.
Depends: H0-2.

**H1-3 · Fail-closed secret key** — Closes S2 · P1 · Est S
`NullCipher` selected silently when `RGNR8_SECRET_KEY` absent; keyless writes are plaintext.
Fix: when any external-credential feature (QBO/webhooks) is enabled, startup refuses without a key; `NullCipher` only under an explicit dev flag.
Files: `packages/qbo/secrets_cipher.py`, composition/startup, `.env.example`.
Acceptance: prod config with QBO enabled and no key → refuse start; test.
Depends: H0-8.

**H1-4 · Atomic single-use verify/reset tokens** — Closes S7 · P1 · Est S
Read-check-then-write TOCTOU allows double-consume.
Fix: consume via conditional `UPDATE … SET consumed=1 WHERE token=? AND consumed=0` in one transaction; act only if a row was affected.
Files: `packages/web/credentials.py`.
Acceptance: concurrent-redeem test → exactly one succeeds.
Depends: none.

**H1-5 · Secure cookies + tighten CSP** — Closes S5 · P1 · Est S
Session cookie lacks `Secure`; CSP allows `unsafe-inline` scripts.
Fix: add `Secure` (behind TLS); remove inline scripts or adopt per-response nonces.
Files: `packages/web` cookie/response code, `middleware.py`, any inline `<script>` in screens.
Acceptance: cookie carries `Secure`; CSP has no `unsafe-inline`; pages still function (E2E).
Depends: none.

**H1-6 · Rate limiter: trusted-proxy XFF + hashed keys** — Closes S5 · P1 · Est S
XFF trusted unconditionally; raw bearer/API-key used as limiter keys.
Fix: trust `X-Forwarded-For` only from a configured proxy; hash the credential before keying.
Files: `packages/web/middleware.py`.
Acceptance: spoofed XFF ignored; limiter keys are hashes; tests.
Depends: none.

**H1-7 · One scoped API-key model** — Closes S8 · P1 · Est M
Live web keys (`rgk_…`) have no scopes; the scoped `rgnr8_apikeys` package is dead code.
Fix: adopt the scoped model in web auth; enforce scopes on each route; migrate existing keys; retire the unscoped path.
Files: `packages/web/apikeys.py`, `app.py` auth path, `packages/apikeys`.
Acceptance: an unscoped-for-X key is refused on an X route; single canonical system; tests.
Depends: none.

**H1-8 · Make Python web-DB RLS real** — Closes P2 · P0 · Est L
Web-DB policies exist (some FORCE'd) but the per-request tenant GUC is never set; `with_tenant` is dead code; `audit_event`/`rgnr8_user` have no policy.
Fix: wrap request DB access to issue `SELECT set_config('app.tenant_id', ?, true)` on the connection before queries; add policies for the uncovered tables; ensure connection pooling doesn't leak context.
Files: `packages/web/sql_store.py`, `audit.py`, `packages/ops/migrate.py`, request/connection wrapper.
Acceptance: cross-tenant read on Postgres returns zero rows; legit requests succeed; RLS isolation test in CI.
Depends: H0-1 (deployable), can run parallel.

---

## Phase 2 — Truth-in-UI & data lifecycle

**H2-1 · Publication validity gate** — Closes A1 · P1 · Est M
Packages seal without checking `inBalance`/reconciliation.
Fix: in the seal/publish path, refuse when the balance sheet is unbalanced or cash flow doesn't reconcile (call the existing `assert*`); require an explicit override with reason + audit for exceptions.
Files: `packages/close/src/financialPackage.ts`, `packageStore.ts`, web publish path.
Acceptance: sealing an unbalanced package raises/refuses; test.
Depends: none.

**H2-2 · Number-provenance labeling in the UI** — Closes trust-boundary theme · P1 · Est L
Owners can't tell forecast vs imported vs posted vs reconciled vs sealed.
Fix: tag each surfaced figure with its state and render a consistent badge/legend; ensure a single snapshot per page (no mixed-moment numbers).
Files: `packages/web` dashboard/statements/screens, ledger-facts snapshotting.
Acceptance: dashboard and statements show state labels; a snapshot test shows one consistent as-of per view.
Depends: none.

**H2-3 · Real erase, or rename it** — Closes P1 · P0 (claim risk) · Est L
`_erase` clears 6 web-local things; leaves ledger, TINs, tokens, users, keys, billing, audit.
Fix: either implement cross-store deletion reaching the ledger (entries/attachments/parties/TINs), QBO tokens, users, API keys, billing — with an audit-retained tombstone — **or** rename to "reset owner data" and remove GDPR/CCPA language until the full workflow exists.
Files: `packages/web/app.py` (`_erase`), ledger-service delete endpoints (new if full erase), marketing/docs.
Acceptance: if full erase — a data-map test shows no residual PII for an erased tenant; if renamed — no endpoint/UI/doc claims GDPR erasure.
Depends: H1-1 (ledger auth) if calling ledger deletes.

**H2-4 · Encrypt/mask vendor TIN/EIN** — Closes P3 · P1 · Est M
`party.tax_id` is plaintext and returned in full.
Fix: encrypt at rest; mask on read (last 4); gate full view behind a permission + audit; add export controls.
Files: `packages/ledger-service/src/documents.ts`, cipher wiring, migration.
Acceptance: DB column is ciphertext; `listParties` returns masked; full view is permissioned + audited; test.
Depends: H1-3 (cipher enforcement).

**H2-5 · Retention exempts close/admin evidence** — Closes P5 · P1 · Est S
Controller can purge audit rows including `close.sealed`, impersonation, membership, erase events.
Fix: add a minimum floor and exempt evidence event types from the sweep; consider moving retention to owner-only or central policy.
Files: `packages/web/audit.py`, `_run_retention`, `rbac.py`.
Acceptance: a purge leaves evidence event types intact; test.
Depends: none.

**H2-6 · Complete webhook SSRF defense + encrypt secrets** — Closes S4 · P1 · Est M
Hostname check doesn't resolve DNS; redirects unchecked; signing secret stored plaintext.
Fix: resolve DNS and block private/link-local ranges; disable redirects or re-validate each hop; encrypt the signing secret via the cipher.
Files: `packages/web/webhooks_out.py`.
Acceptance: a hostname resolving to a private IP is rejected; a redirect to an internal target is blocked; secret stored as ciphertext; tests.
Depends: H1-3.

**H2-7 · Upload malware scanning + quarantine** — Closes P4 · P1 · Est M
No AV/quarantine on receipt uploads (download vector already mitigated by nosniff/attachment).
Fix: scan on upload (e.g. ClamAV seam), quarantine on hit, add an object-access policy; keep the existing download hardening.
Files: `packages/ledger-service/src/attachments.ts`, a scanner seam.
Acceptance: an EICAR test file is quarantined and not downloadable; test.
Depends: none.

**H2-8 · One-time, session-bound QBO OAuth state** — Closes S3 · P1 · Est S
State is HMAC+TTL only; replayable within 15 min; not session-bound.
Fix: add a server-side nonce consumed once, and bind state to the initiating browser session.
Files: `packages/qbo/service.py`, web connect flow.
Acceptance: a replayed state is rejected; test.
Depends: none.

---

## Phase 3 — Accounting controls to system-of-record grade

**H3-1 · Atomic go-live incl. chart persistence** — Closes A2 · P1 · Est L
`executeGoLive` builds the COA in memory and returns it; the worker discards it; post+lock aren't transactional.
Fix: persist the chart in the same transaction as the opening entry and period lock; make the worker persist it; wrap post+lock atomically.
Files: `packages/ledger-kernel/src/goLive.ts`, `goLiveWorker.ts`, `cutover.ts`.
Acceptance: worker-driven go-live → `chart(tenant)` returns the accounts; a crash-injection test leaves a consistent state (no orphan postings).
Depends: H0-7 (durable go-live) helps.

**H3-2 · Cutover locks all prior periods** — Closes A3 · P1 · Est S
Locks only the cutover month though it claims to lock everything prior.
Fix: lock every period ≤ cutover (or explicitly define and enforce the intended horizon); fix the docstring/record.
Files: `packages/ledger-kernel/src/cutover.ts`.
Acceptance: a posting to a pre-cutover month is rejected after cutover; test.
Depends: none.

**H3-3 · Idempotency fingerprint includes dimensions/line memo/provenance** — Closes A4 · P1 · Est S
Fingerprint omits dimensions, line memo, provenance → a materially changed re-post silently returns the original.
Fix: include those fields in the fingerprint so a differing payload raises `DuplicateIdempotencyKeyError`.
Files: `packages/ledger-kernel/src/postingEngine.ts`.
Acceptance: reusing a key with changed dimensions raises; identical retry still dedupes; tests.
Depends: none.

**H3-4 · Fix accumulated-depreciation cash-flow classification** — Closes A5 · P1 · Est M
1510 carries `FIXED_ASSET`, so its movement lands in investing instead of an operating non-cash add-back; `reconciled` stays true, masking it.
Fix: classify accumulated depreciation (contra-asset) so depreciation is an operating add-back in the indirect method.
Files: `packages/ledger-kernel/src/coaTemplates.ts`, `financial-statements/src/statements.ts`, `subtypeClassifier.ts`.
Acceptance: a period with depreciation shows the add-back in operating; investing no longer absorbs it; test.
Depends: none.

**H3-5 · Durable, authoritative close state machine** — Closes A6 · P1 · Est XL
"Closed" lives in three places; the web publish path seals nothing durable and never locks the period.
Fix: one authoritative state; the publish action locks the period, builds and persists a `FinancialPackage`, and enforces SoD + a reopen-approval workflow; derive board tasks from real recon/control results, not an in-memory flag.
Files: `packages/close/src`, `packages/web/app.py` close/publish path, `ledger_client` lock, `financial_package`.
Acceptance: publishing creates a durable package + period lock; reopen requires approval; evidence preserved; tests.
Depends: H2-1.

**H3-6 · Governed schema migrations only** — Closes A9 · P1 · Est L
Service runs raw `CREATE TABLE IF NOT EXISTS` per store at boot; the governed `runMigrations` is never called; no safe ALTER path.
Fix: route all schema through `@rgnr8/migrations` with checksum/version tracking; convert store DDL into versioned migrations; remove boot-time raw DDL.
Files: `packages/ledger-service/src/backend.ts`, store `migrate()` methods, `packages/migrations`, `ledger-postgres` migrations.
Acceptance: schema drift is checksum-guarded; a column ALTER ships as a governed migration; CI migration test.
Depends: H0-3.

**H3-7 · First-class gross profit / margin** — Closes A7 · P2 · Est S
COGS folded into a single expenses bucket.
Fix: split COGS vs operating expense; add gross profit and gross margin lines.
Files: `packages/financial-statements/src/statements.ts`, `accounts.ts`.
Acceptance: income statement exposes `grossProfit`/`grossMargin`; test.
Depends: none.

**H3-8 · Deepen multi-currency** — Closes A8 · P2 · Est L
Framework exists (FX table, ASC 830 remeasurement, CTA translation); gaps are depth.
Fix: translate P&L at period-average rate; carry CTA forward across periods; add a per-line rate audit trail; scope native foreign-currency transactions if needed.
Files: `packages/ledger-service/src/consolidation.ts`, `ledger-kernel/fx.ts`, statements.
Acceptance: translated P&L uses average rate; CTA rolls forward; rate audit present; tests.
Depends: none.

---

## Phase 4 — Product narrowing & re-verification (strategic, parallel)

**H4-1 · Define and ship narrow v1 scope** — Est M
Fix: scope v1 to cash clarity + guided close + accountant handoff; feature-flag or hide the rest and label it provisional in-app.
Acceptance: a scope doc + feature flags; the app presents only v1 as production, the rest as clearly provisional.
Depends: H2-2.

**H4-2 · Marketing claims audit** — Est S
Fix: build a claims register mapping each public claim ("automated accounting," "books you can trust," "compliance-ready," "real-time," "erasure") to a verified capability; remove or qualify unsupported claims.
Acceptance: every live claim maps to a passing acceptance gate.
Depends: Phases 0–3 in progress.

**H4-3 · Independent re-verification** — Est M
Fix: re-run this verification (ideally a fresh reviewer) after Phases 0–3.
Acceptance: all P0/P1 findings show Closed with evidence; no new P0s.
Depends: Phases 0–3.

---

## Suggested milestones

- **M1 "Boots as one system"** = Phase 0 complete (H0-1…H0-10). Gate for any internal demo.
- **M2 "Safe for real data"** = Phase 1 complete + H2-3 (erase honesty) + H2-4 (TIN). Gate for a design-partner with real books.
- **M3 "Controller-trustworthy"** = Phase 2–3 complete. Gate for calling output "financial results."
- **M4 "Launch-ready (narrow)"** = Phase 4 + independent re-verify. Gate for public/paid launch of the narrow product.
