# RGNR8 — v1 Scope (the narrow product)

_Closes H4-1. Companion to `RGNR8-hardening-plan.md` and `RGNR8-hardening-progress.md`. Last updated: 2026-08-20._

## Why narrow

The external deep-review's headline stands: the domain engine is genuinely strong — integer money, append-only posting, reversal-only correction, immutable fingerprinted packages, real FX — but everything at the *trust boundaries* was immature. Phases 0–3 hardened deployment, identity, tenant isolation, data lifecycle, and the close/publication controls. That does not mean every screen is now something an owner should file taxes against.

So v1 is deliberately narrow. We ship, as production, only the surfaces that are now hardened end to end and that deliver the core promise — **know your cash, close the month with confidence, and hand a clean package to your accountant.** Everything else still works and is still visible, but it is labelled **provisional** in the product so nobody mistakes an immature screen for a system of record.

## The v1 production surface

Three jobs, all backed by hardened infrastructure:

**1. Cash clarity.** The 13-week cash outlook, the weekly briefing, scenario what-ifs, receivables/collections, the health view, and the reports surface. These read from the same posted ledger the statements do, and the provenance labels (H2-2) make explicit which figures are forecast and which are fact.

**2. Guided close.** The month-end close board, the books/statements screen, and the sealed financial package. Close now runs through the authoritative, durable state machine (H3-5): the publish action runs the control gate on real results, durably locks the period, and persists an immutable, fingerprinted package — with separation of duties and an approved, two-step reopen.

**3. Accountant handoff.** The connectors/integrations surface (QBO in, webhooks out) and the sealed package the accountant receives — a fingerprinted, verifiable record of a locked period.

**Account administration** (Team, Settings, Audit) is infrastructure and is always production.

### Production nav suffixes

`""` (Cash) · `ask` · `briefing` · `scenarios` · `receivables` · `health` · `reports` · `books` · `close` · `packages` · `connect` · `integrations` · `team` · `settings` · `audit`

This list is the single source of truth in code: `packages/web/src/rgnr8_web/scope.py` → `V1_PRODUCTION`.

## Provisional (works, but not yet production)

The broader accounting surface — the parts the review flagged as immature at the trust boundaries — remains available but is provisional: **Transactions (bank feed), Invoices, Bills, Jobs, Estimates, Pipeline, Capture, Payroll, Debt, Assets.** They are useful and improving, but they should not yet be relied on for decisions or filings.

## How the product enforces this

`scope.py` is the one place the line is drawn; the app shell (`shell.py`) reads it:

- **Nav badge.** Every provisional nav item carries a small dot (`rg-beta`) so, at a glance, the nav shows which surfaces are production and which are not.
- **Provisional banner.** Every provisional screen's body is topped with a one-line disclosure (`scope.PROVISIONAL_NOTE`): _"This is a provisional feature — it works, but it is not yet production-grade and should not be relied on for decisions or filings. For numbers you can act on, use Cash, the Close, and your accountant package."_
- **Hide flag.** `render_shell(..., hide_provisional=True)` removes provisional surfaces from the nav entirely, for a deployment that wants only the v1 surface visible.

Tests: `packages/web/tests/test_scope.py` — production screens render clean; provisional screens are bannered and nav-badged; `hide_provisional` drops them from the nav.

## Graduation criteria (how a provisional surface becomes v1)

A provisional surface graduates to production when: its trust-boundary risks are closed with evidence (the same bar Phases 0–3 held the core to), its numbers carry correct provenance labels, and it has an acceptance gate in the plan that passes. Track graduations here as they happen.
