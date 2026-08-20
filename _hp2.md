# RGNR8 — Hardening Progress

_Companion to `RGNR8-hardening-plan.md` (the ticket list) and `RGNR8-review-verification.md` (the findings). Tracks what's built. Last updated: 2026-08-20._

## Phase 0 — one deployable, bootable topology ✅ COMPLETE

All 10 tickets (H0-1…H0-10). Commit `0d9905f`.
- Image boots: `_pathsetup` globs every package; Dockerfile copies all packages + installs requirements (psycopg, cryptography). `entry:application` imports and `/ready`→200.
- One composition (`render_app.py` → thin alias of `entry.py`); browser login wired (session_secret + credential AuthService); ledger client wired from env; worker email transport real; onboarding persisted (`SqlOnboardingRegistry`).
- Ledger service image (`Dockerfile.ledger`) + compose topology `db → ledger(healthy) → web`; ledger migrates on boot.
- CI: `docker-smoke` job (build + boot + `/ready` + web↔ledger + pg_dump→restore drill); `npm ci`; advisory dependency-audit.

## Phase 1 — security & isolation ✅ COMPLETE

All 8 tickets + the CSP follow-up. Commits `0a86deb`, `5a8a23b`, `5e9ce38`, `c99408d`, `b01625e`.
- **H1-1 (P0)** ledger boundary: per-tenant HMAC tokens (`tenantAuthToken`), auth mandatory (refuse start without token). Client derives the per-tenant token per path.
- **H1-8 (P0)** Python web-DB RLS made real: per-request `app.tenant_id` GUC (`rgnr8_runtime.apply_tenant`) wired into `SqlTenantStore` + membership; migration v5 relaxes the cross-tenant backend tables (fleet_tenant, briefing_subscription); Postgres-gated isolation test runs in CI (Python job now has a Postgres service).
- **H1-2** ledger internal-only (no published port). **H1-3** fail-closed secret key (QBO+DB requires `RGNR8_SECRET_KEY`). **H1-4** atomic single-use tokens (`consume()` conditional UPDATE). **H1-5** Secure cookies + **H1-5b** CSP script nonces (no `unsafe-inline`). **H1-6** rate limiter hashes credentials + ignores untrusted XFF. **H1-7** one scoped API-key model (per-key scopes on the web keys; standalone `rgnr8_apikeys` marked superseded).

Test posture after Phase 1: TS ledger-service 422 · web 583 · ops 118 (+1 PG-gated) · runtime 12 · mcp 6 — all green; mypy `--strict` + ruff + biome + tsc clean. RLS enforcement + Docker boot verified in CI (no Postgres/Docker-registry in the build sandbox).

## Next: Phase 2 — truth-in-UI & data lifecycle (not started)

Per the plan: H2-1 statement/publication validity gate (block sealing an unbalanced package), H2-2 number-provenance labels (forecast/imported/posted/reconciled/sealed), H2-3 honest "erase" (real cross-store deletion or rename), H2-4 encrypt/mask vendor TIN/EIN, H2-5 retention exempts close/admin evidence, H2-6 webhook SSRF (DNS + redirect hops) + encrypt secrets, H2-7 upload malware scan, H2-8 one-time session-bound QBO OAuth state.

## Push mechanics
Cloud session holds no GitHub token; pushes go through a refreshed `~/Downloads/rgnr8-repo.bundle` that Jason fetches into his clone and pushes:
```
git fetch "$HOME\Downloads\rgnr8-repo.bundle" admin-controls-and-operations
git push origin FETCH_HEAD:main
```
