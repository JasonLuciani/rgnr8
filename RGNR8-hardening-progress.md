# RGNR8 — Hardening Progress

_Companion to `RGNR8-hardening-plan.md` (the ticket list) and `RGNR8-review-verification.md` (the findings). Tracks what's built. Last updated: 2026-08-20._

## Phase 0 — one deployable, bootable topology ✅ COMPLETE

All 10 tickets (H0-1…H0-10). Commit `0d9905f`.
- Image boots; one env-gated composition; browser login, ledger client, worker email, onboarding persistence wired; ledger service image + compose topology; CI docker-smoke + migration/restore drill.

## Phase 1 — security & isolation ✅ COMPLETE

All 8 tickets + CSP follow-up. Commits `0a86deb`, `5a8a23b`, `5e9ce38`, `c99408d`, `b01625e`.
- **H1-1 (P0)** per-tenant HMAC ledger auth, mandatory. **H1-8 (P0)** real web-DB RLS (per-request GUC + migration v5 + CI Postgres isolation test).
- H1-2 ledger internal-only · H1-3 fail-closed secret key · H1-4 atomic single-use tokens · H1-5 Secure cookies + H1-5b CSP nonces · H1-6 rate-limiter hashing + XFF · H1-7 one scoped API-key model.

## Phase 2 — truth-in-UI & data lifecycle 🟩 7 of 8 COMPLETE

Commits `a708bbf`, `1cbbf1d`, `d00ae0e`, `d22b696`, `7ddc55b`.
- **H2-1** publication validity gate: `buildFinancialPackage` refuses to seal books that don't tie out (explicit auditable override only); store rejects invalid content too.
- **H2-3** honest "erase": relabeled "reset owner-held data"; dropped the GDPR/CCPA claim; response + UI disclose exactly what is retained (ledger, tax IDs, tokens, users, keys, billing, audit).
- **H2-4** vendor TIN: AES-256-GCM encryption at rest (keyed from `RGNR8_SECRET_KEY`) + last-4 masking on screen.
- **H2-5** retention exempts control/close evidence (protected prefixes) + a 90-day floor.
- **H2-6** webhook SSRF: DNS resolution + block private/metadata; no-follow redirects; signing secret encrypted at rest.
- **H2-7** upload content-scanner seam (quarantine-by-rejection; default allow-all, real AV injected in prod).
- **H2-8** one-time, replay-proof QBO OAuth state (nonce + injectable store).

**Remaining: H2-2** — number-provenance labels in the UI (tag each figure forecast / imported / posted / reconciled / sealed so an owner knows which number is safe to act on). This is a broad, cross-cutting UI feature rather than a security/correctness fix; deferred for focused design.

Test posture after Phase 2 (so far): ledger-service 427 · close 44 · web 591 · ops 118 (+1 PG) · qbo 31 · runtime 12 · mcp 6 — all green; mypy `--strict` + ruff + tsc + biome clean.

## Phase 3 — accounting controls to system-of-record grade 🟩 7 of 8 COMPLETE

Commits `eb29a67`, `d279832`, `ad5bc16`, `e24fc99`, `1467218`, `8dd10bb`, `56a19b2`.
- **H3-1 (A2)** atomic go-live that persists the chart. New `ChartStore` seam +
  `TransactionRunner`; chart written before the opening entry, all in one unit;
  worker + service handler persist it (no post-hoc save loop). `PgAccountStore`
  writes a chart in one transaction. Crash-injection tests prove no orphan
  postings and idempotent resume.
- **H3-2 (A3)** cutover freezes *every* period through the cutover month via a new
  `PeriodStore.lockThrough` high-water mark (in-memory watermark + SQL
  `LOCKED_THROUGH` rows); docstring/`CutoverRecord` corrected.
- **H3-3 (A4)** idempotency fingerprint now covers per-line dimensions, per-line
  memo, and provenance (post + reverse); identical retries still dedupe.
- **H3-4 (A5)** accumulated depreciation reclassified to an operating non-cash
  add-back (new `ACCUMULATED_DEPRECIATION` subtype + name fallback).
- **H3-6 (A9)** all schema now flows through the governed, checksum-tracked
  migration runner (`migrations.ts` → `runMigrations`); drift is rejected; a
  column ALTER ships as a new versioned migration. CI covers it.
- **H3-7 (A7)** first-class gross profit / margin: P&L splits COGS from operating
  expense (by subtype) and exposes `grossProfit`/`grossMargin` in JSON + render.
- **H3-8 (A8)** multi-currency depth: `translateTrialBalance` translates P&L at
  the period-average rate, equity at historical, monetary at current; CTA rolls
  forward (cumulative − prior); per-line rate audit in the consolidate() output.

Remaining Phase 3: **H3-5** durable close/lock/package/reopen state machine with
SoD + evidence preservation (A6) — the XL ticket, in progress.

## Also outstanding
- **H2-2** (provenance labels in the UI) — deferred Phase 2 UI feature.

## Push mechanics
Cloud session holds no GitHub token; pushes go through a refreshed `~/Downloads/rgnr8-repo.bundle` that Jason fetches into his clone and pushes:
```
git fetch "$HOME\Downloads\rgnr8-repo.bundle" admin-controls-and-operations
git push origin FETCH_HEAD:main
```
