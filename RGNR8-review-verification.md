# RGNR8 — Independent Verification of the External Deep Review

_Date: 2026-08-20. Method: four independent reviewers, each tracing the claims against the actual source (Python `packages/*/src` and the TypeScript ledger service `packages/ledger-service/src` + `ledger-kernel`, `financial-statements`, `close`), citing file:line evidence. This document records which findings hold up, which are overstated, and what to do next._

## Verdict

**The external review is credible and should be acted on.** Of 29 specific findings verified, **24 are confirmed, 5 are partially true (the reviewer over- or under-stated a sub-claim), and 0 could not be reproduced.** All seven P0 release-blockers are confirmed. In several cases the reviewer *understated* the problem (e.g. the ledger is not only un-deployed but never even wired into the production composition; the shared ledger token can be absent entirely).

The headline conclusion stands: **do not launch as a production financial platform yet.** The domain engine (integer money, append-only posting, reversal-only correction, immutable fingerprinted packages, real FX machinery) is genuinely strong. What is immature is everything at the trust boundaries — deployment, identity, tenant isolation on the web tier, data lifecycle, and close/publication controls. Recent feature work (including Ask RGNR8) added surface area on a base that does not yet deploy as one system.

One correction to our own `RGNR8-project-state.md`: its claim that "row-level security is actually enforced" is true **only for the TypeScript ledger/subledger Postgres database.** The Python web app's own database defines RLS policies but never sets the per-request tenant context, so they are inert (and would break the app if forced). That doc should be corrected.

## Scorecard

| # | Finding | Sev (review) | Verdict | One-line evidence |
|---|---|---|---|---|
| D1 | Docker image omits required packages | P0 | **Confirmed** (understated) | `Dockerfile:24-29` copies 5 pkgs; `entry.py` import chain needs alerts, obs, ar, billing, categorize, qbo, reports, scenario, ocr, copilot + pip deps reportlab/openpyxl/cryptography |
| D2 | Ledger service not deployed/migrated/wired | P0 | **Confirmed** (understated) | image has no Node; `migrate.py` runs Python only; no compose/Procfile boots it; prod never calls `set_ledger` (only `run_local.py:127`) |
| D3 | Email delivery is a no-op | P0 | **Confirmed** | `worker.py:30-35` prints "would construct" then `return RecordingDeliverer()`; real `ProviderDeliverer`/`HttpEmailTransport` exist but unwired |
| D4 | Default browser login not wired | P0 | **Confirmed** | `app_factory` never passes `session_secret`; hs256 → `_sso_mode()`=True; `POST /login` returns 400 "self-issued login disabled"; no IdP flow either |
| D5 | Go-live/onboarding state in memory | P1 | **Confirmed** | `onboarding.py:210-213` plain dicts; no Sql variant; TS `InMemoryGoLiveQueue` mirrors it |
| D6 | Multiple conflicting production paths | — | **Confirmed** | `entry.py` (durable/RBAC/no-login/no-ledger) vs `render_app.py` (in-memory/login/qbo, would crash in image) vs `run_local.py` (only one wiring ledger) |
| D7 | CI doesn't build/boot image; `npm install` not `ci`; no SBOM/E2E/restore/smoke | — | **Confirmed** | `.github/workflows/ci.yml`: source-level lint/type/unit + Postgres only |
| S1 | Ledger API: one shared bearer for all tenants/actions | P0 | **Confirmed** (understated) | `handlers.ts:423-432` one constant-time token, tenant from URL path; auth **off** when token empty (`bin.ts:30,75`) |
| S2 | QBO tokens can be plaintext; key optional | P1 | **Confirmed** | `secrets_cipher.py:89-94` → `NullCipher` when key absent; no fail-closed startup; `.env.example:28` key commented out |
| S3 | QBO OAuth state replayable | P1 | **Confirmed** | `service.py:79-101` HMAC+15min TTL, no nonce/one-time/session binding (real exploit is CSRF/fixation; code is single-use at Intuit) |
| S4 | Webhook SSRF incomplete; secrets plaintext | P1 | **Confirmed** | `webhooks_out.py:33-60` no DNS resolve; `urlopen` follows redirects unchecked; `:207-212` secret stored verbatim |
| S5 | Cookies no Secure; CSP unsafe-inline; XFF trusted; raw secrets as RL keys | P1 | **Confirmed** (4/4) | `app.py:4048` no Secure; `middleware.py:156` `unsafe-inline`; `:69-72` XFF; `:61-68` `key:{api_key}`/`principal:{token}` |
| S6 | Support impersonation default-on, unattributed | P1 | **Partial** | default-on confirmed (`platform.py:37`); **attribution overstated** — subject recorded+audited (`:157-163`); missing = case binding + impersonation flag |
| S7 | Password/token ops race-prone; hash params unbounded | P1 | **Partial** | TOCTOU double-consume **confirmed** (`credentials.py:384-441`, no `WHERE consumed=0`/txn); hash-bounds overstated (maxmem cap; low sev) |
| S8 | Two incompatible API-key systems | P1 | **Confirmed** | live web `rgk_…` no scopes (`apikeys.py`); scoped `rgnr8_…` package is dead code (imported nowhere) |
| P1 | "Right to erase" leaves financial/customer data | P0 | **Confirmed** | `app.py:5274-5314` clears 6 web-local things; never touches ledger, TINs, tokens, users, keys, billing, audit |
| P2 | Python-side RLS not applied | P0 | **Partial** | TS ledger DB **is** enforced (`set_config` in ~20 modules); Python web DB policies exist but GUC never set (`with_tenant` dead code) → inert / would break app if forced |
| P3 | Vendor TIN/EIN stored + returned plaintext | P1 | **Confirmed** | `documents.ts:181,275-306` `tax_id text`, returned in full by get/listParties, no masking |
| P4 | Uploads: no malware/content scanning | P1 | **Partial** | no AV/quarantine **confirmed** (`attachments.ts:342-376`); download vector **mitigated** by forced `attachment` + `nosniff` |
| P5 | "Append-only" audit deletable via retention | P1 | **Confirmed** | `audit.py:146-161` hard DELETE; `_run_retention` (`app.py:5329-5347`) controller-triggerable, no evidence exemption |
| A1 | Statements can publish when unbalanced | P1 | **Confirmed** | `financialPackage.ts:119-133` fingerprints content, never checks `inBalance`/`inAgreement`; `assert*` gates uncalled |
| A2 | Go-live not atomic; worker never persists chart | P1 | **Confirmed** | `goLive.ts:133-162` builds COA in memory, returns it; `goLiveWorker.ts:74-102` discards it; `cutover.ts:159,166` post+lock not transactional |
| A3 | Cutover locks only the cutover month | P1 | **Confirmed** | `cutover.ts:165-166` locks one period; docstring claims "every period ≤ cutover"; earlier months stay postable |
| A4 | Idempotency fingerprint omits dimensions/memo/provenance | P1 | **Confirmed** | `postingEngine.ts:153-167` hashes account/side/currency/units + entry memo only; re-dimensioned re-post silently returns original |
| A5 | Accum. depreciation as fixed asset → cash-flow misclass | P1 | **Confirmed** | `coaTemplates.ts:60` 1510 `FIXED_ASSET`; classifiers route to investing (`statements.ts:194`, `subtypeClassifier.ts:21`); reconciled stays true, masking it |
| A6 | No durable authoritative close state machine | P1 | **Confirmed** | 3 notions of "closed"; web `CloseBoard.sealed` in-memory (`app.py:546`), `_close_publish` seals nothing durable, never locks period |
| A7 | P&L lacks first-class gross profit/margin | P2 | **Confirmed** | `statements.ts:49-71` only revenue/expenses/netIncome; COGS folded into expenses |
| A8 | Multi-currency incomplete | P2 | **Partial (mostly misread)** | FX rate table, ASC 830 remeasurement, CTA translation all exist (`fx.ts`, `consolidation.ts:540-577`); gaps are depth: current-not-average rate, CTA not carried forward, no per-line rate audit, single-currency entries |
| A9 | Boot-time DDL bypasses migration governance | P2 | **Confirmed** | `backend.ts:378-439` runs raw `CREATE TABLE IF NOT EXISTS` per store; governed `runMigrations` exists but is never called by the service |

**Totals: 24 Confirmed · 5 Partial · 0 Not reproduced.** All 7 P0s confirmed.

## What the reviewer got wrong or overstated

The review is not flawless — three sub-claims are softer than stated, which matters for prioritization:

1. **Support impersonation "not attributed" (S6)** — false. The minted view-as token's subject is the support user, the mint is audited, and downstream web mutations record the subject. The real gaps are narrower: no support-case/ticket binding, and audit rows aren't flagged as impersonated. Lower urgency than "unattributed cross-tenant access."
2. **Password hash params "unbounded" (S7)** — overstated. `scrypt` runs with a `maxmem` cap and the encoding is server-generated. The *race* (token double-consume) is the real, confirmed issue; the hash-bounds part is minor defense-in-depth.
3. **Multi-currency "incomplete … no framework" (A8)** — largely a misread. An FX rate table, ASC 830/IAS 21 remeasurement with a balanced gain/loss journal, and a CTA translation path into consolidation all exist and are tested. The genuine gaps are depth (average vs current rate for P&L, CTA not carried across periods, no per-line rate audit trail, no native foreign-currency transactions) — a "harden," not a "build."

Everything else the reviewer flagged is real, and two P0s are worse than described (D2 ledger not even wired; S1 auth can be entirely off).

## Recommended recovery sequence

Feature work should pause for a deliberate hardening phase. Suggested order, each phase gated by a concrete acceptance test so "done" is verifiable:

**Phase 0 — Make it real and safe to boot (the deployability P0s).**
One deployable topology: web + ledger API (Node) + worker + migrations (Python *and* TS) + DB + secrets + health. Fix the Dockerfile package/dep set; add a **CI gate that builds and boots the image and hits `/ready`** (this single gate catches D1/D2/D6). Wire `ProviderDeliverer` (D3). Wire a working login path — either a real SSO flow or `session_secret`+credentials in the composition (D4). Persist onboarding/go-live state (D5). Collapse `entry.py`/`render_app.py` into one composition. Acceptance: `docker compose up` serves an authenticated owner request end-to-end, and the ledger is actually wired.

**Phase 1 — Close the security/isolation P0s before any real tenant data.**
Per-tenant authorization at the ledger boundary (kill the single shared token; make auth mandatory) (S1). Fail-closed secret-key enforcement at startup (S2). Set the per-request tenant GUC on the Python web DB — or drop the FORCE'd policies until it's wired — so isolation is real, not inert (P2). Atomic single-use verify/reset tokens (S7). Secure cookies, tighten CSP, stop trusting XFF, stop using raw credentials as rate-limit keys (S5). One scoped API-key model (S8).

**Phase 2 — Truth-in-UI and data lifecycle.**
Block unbalanced/unreconciled statements from being *presented or sealed* as final (A1). Label every number's state (forecast / imported / posted / reconciled / sealed) so an owner knows what's safe to act on. Make "erase" either a real cross-store deletion reaching the ledger, or rename it and stop claiming GDPR/CCPA (P1). Encrypt/mask vendor TIN/EIN (P3). Exempt close/admin evidence from retention purges (P5). Complete webhook SSRF (DNS + redirect hops) and encrypt webhook secrets (S4). Add upload AV/quarantine (P4).

**Phase 3 — Accounting controls to system-of-record grade.**
Atomic go-live incl. chart persistence (A2). Cutover that locks all prior periods as claimed (A3). Idempotency fingerprint that includes dimensions/line memo/provenance (A4). Fix accumulated-depreciation cash-flow classification (A5). A single durable close/lock/package/reopen state machine with SoD and evidence preservation (A6). Route all schema changes through the governed migration runner (A9). Add first-class gross profit (A7) and deepen FX (average-rate P&L, carried-forward CTA) (A8).

**Phase 4 — Product narrowing (parallel, strategic).**
Ship a narrow first product — cash clarity + guided close + accountant handoff — with everything else clearly labeled provisional. Restrain marketing claims ("automated accounting," "books you can trust," "compliance-ready," "real-time," "erasure") until the blockers are fixed and independently re-verified.

**Then** resume feature work on the hardened base.

## CI gates to add (each would have caught a confirmed finding)
Docker build + boot smoke; DB migrations (Python + TS) applied against a fresh DB; ledger↔web integration; QBO mock OAuth flow; worker delivery (real transport, mock HTTP); Python-web RLS isolation test (cross-tenant read returns zero); backup→restore drill; browser E2E for login + one owner workflow; `npm ci` + dependency/SBOM scan.
