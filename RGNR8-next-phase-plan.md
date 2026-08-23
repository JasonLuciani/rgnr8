# RGNR8 — Next-Phase Plan (APPROVED 2026-08-20)

_Approved after Phases 0–4 (all P0/P1 findings Closed). All three tracks are green-lit: **A** agent-native accounting (FinStat-inspired parity), **B** close out the tracked hardening residuals from the H4-3 re-verification, **C** graduate provisional surfaces into v1 as they harden. Companion to `RGNR8-hardening-plan.md`, `RGNR8-reverification.md`, `RGNR8-v1-scope.md`._

**Decisions locked (2026-08-20):** do all three tracks in full. Usage-based LLM pricing is deferred to a later version (see Backlog). Sequence per "Suggested sequencing" below.

Effort key: S ≈ ½–1 day · M ≈ 1–3 days · L ≈ 3–6 days · XL ≈ 1–2 wks.

---

## Track A — Agent-native accounting (mirror what's worth mirroring from finstat.ai)

The wedge: RGNR8 already has the engines (`ocr`, `ingestion`, `categorize`, `ledger-service`, `statements`/`reports`, `qbo-migrate`); the MCP surface only exposes 5 read tools. Turn the pipeline itself into a safe, RBAC-gated, agent-callable toolset. Every write still flows through the same `WebApp` auth + tenant-scoping + authoritative close controls — the trust boundary FinStat markets but doesn't demonstrate.

- [ ] **A-1 · Agent-native Parse→Match+Post→Report→Export MCP catalog** — Track A · P1 · **L/XL**
  Expand `packages/mcp` from 5 read tools to a full pipeline: `parse_document` (front `ocr`/`ingestion`: PDF/CSV/image → structured txns), `suggest_categorization` + `post_entries` (front `categorize` + ledger post, balanced-journal + closed-period enforced), `reconcile` (front `reconciliation`), `report` (P&L / recon / txn detail / MoM from `statements`), `export` (QBO/CSV via `qbo-migrate`/`connectors`). Each tool is a thin adapter over the existing `WebApp` Request path, so it inherits RBAC + scoped `rgk_` keys + tenant isolation; write tools require the same permissions as the UI.
  Files: `packages/mcp/src/rgnr8_mcp/server.py` (+ tool schemas), thin handlers reusing existing web/ledger endpoints.
  Acceptance: an agent can parse a statement, post balanced entries into a closed-period-aware ledger, pull a P&L with every number source-traceable, and export QBO — all gated by the caller's scopes; a scope-insufficient key is refused. Tests per tool.
  Depends: none (engines exist).

- [ ] **A-2 · "Split one commingled account into two sets of books" flow** — Track A · P2 · **M**
  A first-class guided flow that takes one bank statement and routes transactions to two charts of accounts (e.g. Household vs Business / Schedule C, or multi-client), reusing `categorize` rules + dimensions. FinStat leads with this; we have the parts but no workflow.
  Files: `packages/categorize`, a new split/routing helper, a web screen + MCP tool.
  Acceptance: from one import, produce two balanced sets of books by rule; the split is auditable and each entry shows which book it landed in. Test.
  Depends: A-1 (shares the parse/categorize tools).

- [ ] **A-3 · Source-traceable "working record" in Ask** — Track A · P2 · **S**
  Surface, in the Ask/copilot answer, the "question any number → see its source entry → correct on the spot" loop explicitly. Mostly packaging on top of the copilot integrity gate + H2-2 provenance labels.
  Files: `packages/copilot`, `ask_screens.py`.
  Acceptance: every dollar figure in an Ask answer links to its posted entry/provenance; a "correct this" affordance routes to the entry. Test that answers carry per-figure source refs.
  Depends: none.

- [ ] **A-4 · OFX export (confirm gap, then close)** — Track A · P3 · **S**
  Verify whether an OFX exporter exists; if not, add one alongside the QBO/CSV export paths (FinStat exports QBO/OFX/CSV).
  Files: `packages/connectors` or `qbo-migrate` export.
  Acceptance: a period exports valid OFX that round-trips into a third-party tool; test against the OFX schema.
  Depends: none.

---

## Track B — Close the tracked hardening residuals (from H4-3 re-verification)

None re-open a P0/P1 finding; each is a depth/wiring gap on top of a working control. Details in `RGNR8-reverification.md`.

- [ ] **B-1 · Wire the durable onboarding/go-live store into production (closes D5 residual)** — Track B · P1 · **M**
  `SqlOnboardingRegistry` exists but no shipped composition constructs the operator app that uses it, so go-live state is still effectively in-memory in a running deploy.
  Files: `packages/ops` composition (`app_factory.py`/`deploy.py`), operator app wiring.
  Acceptance: a booted deployment persists go-live/onboarding state across restart; a restart test shows state survives. Depends: none.

- [ ] **B-2 · Harden support impersonation (closes S6 residual)** — Track B · P1 · **M**
  Impersonation is audited + staff-attributed, but it is default-on, the `view_as_enabled` kill switch only guards role-emulation (not plain `impersonate()`), and there is no case/ticket binding.
  Files: `packages/ops/src/rgnr8_ops/platform.py`.
  Acceptance: `impersonate()` is gated by the same kill switch, requires a case/ticket id, and impersonated audit rows are flagged as such; tests for the off-switch and required-case-id. Depends: none.

- [ ] **B-3 · Make go-live atomic on the HTTP path (closes A2 residual)** — Track B · P2 · **M**
  The goLive handler passes a `chartStore` but no `transaction` runner, so chart-persist → opening-post → period-lock are sequential (safe-to-resume via idempotency, but not one transaction).
  Files: `packages/ledger-service/src/handlers.ts`, a Postgres transaction runner seam.
  Acceptance: a mid-go-live failure rolls back all three steps as one unit (real Postgres integration test). Depends: none.

- [ ] **B-4 · Real recon results feed the close gate (closes A6 residual)** — Track B · P2 · **M**
  On the web publish path the gate's `controls` are synthesized from close-board task `done` flags, not independent reconciliation results — partly circular.
  Files: `packages/web` close path, `reconciliation`/`recon-monitor` wiring into the publish call.
  Acceptance: publishing is gated by actual per-account reconciliation + control-tie results pulled from the ledger, not self-reported flags; test a failing recon blocks the seal. Depends: none.

- [ ] **B-5 · Operational hardening (CI + multi-worker)** — Track B · P2 · **M**
  Three items the re-verification flagged: (a) the CI restore drill doesn't verify row counts/objects — make it assert a restored invariant; (b) the dependency audit is `continue-on-error` — make high-severity advisories block; (c) the OAuth nonce store and rate limiter are per-process — provide a shared (DB/Redis) store so replay-detection and limits hold across web workers.
  Files: `.github/workflows/ci.yml`, `packages/qbo` nonce store, `packages/web/middleware.py`.
  Acceptance: restore drill fails on data loss; a seeded high-severity advisory fails CI; nonce/limit state is shared across two workers in a test. Depends: none.

---

## Track C — Graduate provisional surfaces into v1

Today's provisional surfaces (labelled in-app per H4-1): **transactions (bank feed), invoices, bills, jobs, estimates, pipeline, capture, payroll, debt, assets.** Each graduates to production when its trust-boundary risks are closed with evidence, its numbers carry correct provenance labels, and it has a passing acceptance gate.

- [ ] **C-0 · Graduation gate template** — Track C · P1 · **S**
  A short, reusable checklist a surface must pass to move from `provisional` to `V1_PRODUCTION` in `scope.py`: (1) inputs validated + tenant-isolated; (2) numbers carry provenance labels; (3) writes flow through authoritative controls (no silent state); (4) an acceptance test proving the above; (5) sign-off recorded here.
  Files: `RGNR8-v1-scope.md` (graduation section), `scope.py`.
  Acceptance: template exists and one surface is graduated through it as the worked example. Depends: none.

- [ ] **C-1 · Graduate Bank feed / Transactions** — Track C · P1 · **M** — the data-in surface most owners live in; highest value to graduate first.
- [ ] **C-2 · Graduate AR (Invoices) + Receivables** — Track C · P1 · **M**
- [ ] **C-3 · Graduate AP (Bills)** — Track C · P1 · **M**
- [ ] **C-4 · Graduate Capture (receipts/OCR)** — Track C · P2 · **S** — pairs with A-1's parse tools.
- [ ] **C-5 · Graduate Payroll** — Track C · P2 · **L** — highest compliance surface; graduate last of the core set.
- [ ] **C-6 · Graduate Jobs / Estimates / Pipeline** — Track C · P3 · **M** — the project layer; graduate as a group.
- [ ] **C-7 · Graduate Debt + Assets** — Track C · P3 · **M**

Each C-1…C-7: pass the C-0 gate for that surface, then remove it from the provisional set. Acceptance per surface = its C-0 checklist all green + tests.

---

## Suggested sequencing (for discussion)

1. **B-4 + B-1** first — they finish the close/go-live story you just built and are quick wins.
2. **A-1** — the headline: the agent-native pipeline. Biggest strategic payoff; the engines already exist.
3. **C-0 + C-1** — graduate the bank feed, the surface owners use most, as the worked example of the graduation gate.
4. Then A-2/A-3/A-4 (agent-surface polish), B-2/B-3/B-5 (remaining residuals), and the rest of Track C as each surface hardens.

## Backlog (later versions)

- [ ] **BL-1 · Usage-based LLM pricing** — an interesting model for metering the AI/copilot surface (à la FinStat's $30/M tokens, no per-seat). Scope for a later version: meter copilot/Ask token usage per tenant, expose it in billing, and offer a usage-based plan alongside the current model. Not in Tracks A–C.
