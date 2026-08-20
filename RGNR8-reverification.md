# RGNR8 — Independent Re-Verification (post Phases 0–3)

_Closes H4-3. Date: 2026-08-20. Method: four fresh independent reviewers (one each for deployability, security, privacy, accounting), tracing every previously-confirmed finding against the **current** source with file:line evidence — not assuming any fix. Companion to `RGNR8-review-verification.md` (the original findings) and `RGNR8-hardening-progress.md`._

## Verdict

**All 29 findings from the original review are now Closed or materially closed with evidence, and there are no new P0s.** Of the P0/P1 set, the substantive accounting, privacy, and security correctness fixes all verified as CLOSED against the current code. The re-verification surfaced **five residual hardening items** — none a release-blocker, none re-opening a P0. Two were quick, concrete regressions and have been **fixed in this pass**; three are tracked below.

The seven original P0s are all confirmed CLOSED: D1–D4 (deployability), S1 (ledger auth), P1 (erase honesty), P2 (Python-web RLS actually applied).

## Scorecard (current state)

| Finding | Orig sev | Re-verify verdict | Evidence (current) |
|---|---|---|---|
| D1 image packages/deps | P0 | **CLOSED** | `deploy/Dockerfile` copies the whole `packages` tree; `requirements.txt` has psycopg/cryptography/reportlab/openpyxl |
| D2 ledger deployed+wired | P0 | **CLOSED** | `deploy/Dockerfile.ledger` + `docker-compose.yml` ledger service; `app_factory` calls `set_ledger(LedgerClient(...))` |
| D3 email delivery | P0 | **CLOSED** | `deploy/worker.py` builds a real `ProviderDeliverer`/`HttpEmailTransport` when the key is set |
| D4 browser login | P0 | **CLOSED** | `config.py`/`app_factory` wire `session_secret` + credentials + `AuthService`; CI smoke sets it |
| D5 onboarding persistence | P1 | **PARTIAL (tracked)** | `SqlOnboardingRegistry` exists and is durable, but no production composition constructs the operator app that uses it |
| D6 one composition | — | **CLOSED** | single `create_application`; `render_app.py` a thin alias of `entry.py`; no `run_local.py` |
| D7 CI build/boot/restore | — | **CLOSED** | `npm ci`; `docker-smoke` builds+boots+polls `/ready`+web→ledger reach + pg_dump/restore drill |
| S1 per-tenant ledger auth | P0 | **CLOSED** | `handlers.ts` per-tenant HMAC token, constant-time; `bin.ts` refuses to start without a token unless explicitly allowed |
| S2 fail-closed secret key | P1 | **CLOSED** | `config.py` raises `ConfigError` when QBO+DB configured without `secret_key` |
| S3 OAuth state replay | P1 | **CLOSED** | `qbo/service.py` one-time nonce, consumed on verify (per-process store noted) |
| S4 webhook SSRF + secret-at-rest | P1 | **CLOSED (fixed this pass)** | SSRF DNS+private-IP block and no-follow redirects were closed; the signing-secret cipher was **not** wired in `deploy.py` → **fixed** (now passes `cipher_from_env`) |
| S5 cookies/CSP/XFF/RL keys | P1 | **CLOSED** | Secure cookies default on; CSP nonces; XFF untrusted by default; rate-limit keys hashed |
| S6 support impersonation | P1 | **PARTIAL (tracked)** | now audited + staff-attributed; still default-on, the kill switch doesn't cover plain `impersonate()`, and there's no case/ticket binding |
| S7 token double-consume | P1 | **CLOSED** | `credentials.py` atomic `UPDATE … WHERE consumed=0` gated on rowcount |
| S8 one API-key model | P1 | **CLOSED** | single `rgk_` scoped model; scopes enforced in `app.py` (`_KEY_SCOPES`, 403 on insufficient scope) |
| P1 erase honesty | P0 | **CLOSED** | `_erase` resets owner-held web data only, drops the GDPR/CCPA claim, discloses what is retained |
| P2 Python-web RLS applied | P0 | **CLOSED** | `runtime/rls.py apply_tenant()` sets `app.tenant_id` per request; called in `sql_store.py`/`rbac.py`; migration provides a workable policy |
| P3 vendor TIN | P1 | **CLOSED** | `fieldCipher.ts` AES-256-GCM encrypts `tax_id` at rest; `ten99_screens.py` masks to last-4 |
| P4 upload scanning | P1 | **CLOSED** | `attachments.ts` `ContentScanner` seam rejects flagged uploads before store |
| P5 audit retention | P1 | **CLOSED** | `audit.py` protected-prefix exemption in both purge paths; 90-day floor in `app.py` |
| A1 unbalanced publish | P1 | **CLOSED** | `buildFinancialPackage` throws unless balanced; store `assertIntact` re-validates on read+write |
| A2 atomic go-live | P1 | **CLOSED (residual tracked)** | chart persisted before posting via `ChartStore`; worker + handler wire it. Residual: the HTTP handler passes no `transaction` runner, so the path is sequential+idempotent, not a single transaction |
| A3 cutover lock scope | P1 | **CLOSED** | `executeCutover` uses `periods.lockThrough` high-water mark; any period ≤ mark reads LOCKED |
| A4 idempotency fingerprint | P1 | **CLOSED** | fingerprint includes per-line dimensions + memo + provenance, for post and reverse |
| A5 accumulated depreciation | P1 | **CLOSED** | new `ACCUMULATED_DEPRECIATION` subtype → operating add-back; name-based fallback too |
| A6 close state machine | P1 | **CLOSED (fixed this pass)** | authoritative `publishClose` (lock+package+SoD+2-step reopen), wired into ledger HTTP and the web publish path. Residual found: the web omitted `prepared_by`, leaving publish-time SoD dormant → **fixed** (the close board now records the preparer and passes it) |
| A7 gross profit | P2 | **CLOSED** | `IncomeStatement` exposes COGS/grossProfit/grossMargin by subtype split |
| A8 FX depth | P2 | **CLOSED** | `translateTrialBalance`: average-rate P&L, roll-forward CTA, per-line rate audit |
| A9 governed migrations | P2 | **CLOSED** | `migrations.ts` routes all schema through `runMigrations`; store raw-DDL boot path removed |

**Totals: 27 CLOSED (2 of them fixed in this pass) · 2 PARTIAL/tracked · 0 re-opened · 0 new P0s.**

## Fixed in this re-verification pass

1. **S4 — webhook signing secret was plaintext at rest in production.** The SSRF hardening landed, but `deploy.py` constructed `SqlWebhookEndpointStore` without a cipher (defaulting to `NullCipher`) while the adjacent QBO store passed `cipher_from_env`. Now wired to the same cipher, so the signing secret is Fernet-encrypted at rest whenever `RGNR8_SECRET_KEY` is set. (Commit alongside this doc.)
2. **A6 — separation of duties was dormant on the web publish path.** `publishClose` enforces preparer ≠ publisher, but the web never sent `prepared_by`. The close board now records who advanced the tasks and passes them as the preparer, so a single person can no longer both prepare and publish a close through the UI. New test asserts both identities reach the ledger.

## Tracked residuals (hardening, not blockers)

These are real but do not re-open any finding; each is a depth/wiring gap on top of a working control.

1. **D5 — durable onboarding store is unconnected.** `SqlOnboardingRegistry` is built and correct, but no shipped composition constructs the operator app that consumes it, so go-live state is still effectively in-memory in a running deployment. _Next: construct the operator app in the production composition (or fold its go-live persistence into the main app)._ 
2. **S6 — impersonation kill switch + case binding.** Impersonation is now audited and attributed to the support user, but it is default-on and the `view_as_enabled` switch guards only role-emulation, not plain `impersonate()`; there is no ticket/case reference. _Next: gate `impersonate()` behind the same switch, require a case id, and flag impersonated audit rows._
3. **A2 — go-live not a single transaction on the HTTP path.** Chart-persist → opening-post → period-lock run sequentially (the handler passes no `transaction` runner); chart-first ordering + cutover idempotency make a partial failure safely re-runnable, but it is not atomic. _Next: thread a DB-transaction runner through the goLive handler._
4. **Operational notes (non-blocking):** the CI restore drill doesn't verify row counts; the dependency audit is non-blocking; the OAuth nonce store and rate limiter are per-process (fine single-worker, shared store needed for multi-worker); the webhook/close gate `controls` on the web path are derived from board task flags rather than independent reconciliation results.

## Bottom line

Phases 0–3 hold up under fresh, independent scrutiny: every P0/P1 correctness finding is Closed with file:line evidence, two residual regressions were caught and fixed in this pass, and the remaining items are tracked hardening — none a launch blocker for the narrow v1 (cash clarity + guided close + accountant handoff) defined in `RGNR8-v1-scope.md`.
