# RGNR8 — Marketing Claims Register

_Closes H4-2. Every public capability claim maps to a verified capability and a passing acceptance gate, or is qualified/removed. Companion to `RGNR8-hardening-plan.md`, `RGNR8-review-verification.md`, `RGNR8-v1-scope.md`. Last updated: 2026-08-20._

## How to read this

A claim is only allowed to appear in public copy (site, app store, sales deck, in-app hero) if it has a row here with a **Verdict** of _Supported_ or _Qualified_ and a named **Gate** that passes. _Remove/Rewrite_ rows are claims we must not make as written; the **Approved wording** column gives the honest version. The **Gate** is the acceptance test or hardened capability that makes the claim true — usually an `H-` ticket from the hardening plan.

This register is the governance artifact H4-2 asks for. When someone drafts new copy, it gets a row here first.

## The register

| # | Public claim (as tempting to write) | Verdict | What actually backs it | Gate (evidence) | Approved wording |
|---|---|---|---|---|---|
| 1 | "Automated accounting — your books post themselves" | **Qualified** | Rule-based bank-feed categorization + opt-in auto-post rules; the inbox shows "posts automatically" only for rules the owner set, everything else is "suggests only". | `@rgnr8/categorize` suggester tests; inbox auto-post path (`inbox_screens.py`) | "Automates the repetitive categorization — you set the rules, confirm the rest." Never "fully automated / hands-off." |
| 2 | "Answers you can trust" / "books you can trust" | **Supported** | The copilot's numeric-integrity gate refuses to emit any dollar figure not tied to the books; append-only posting + reversal-only correction; immutable fingerprinted packages; provenance labels tell fact from forecast. | copilot orchestrator integrity gate + tests; **H2-1** publication validity gate; **H2-2** provenance labels | Keep. Pairs well with "every number is traceable to a posted entry." |
| 3 | "Compliance-ready" / "audit-ready, out of the box" | **Remove/Rewrite** | We keep a real audit trail (append-only ledger, audit events, control-evidence retention) and enforce separation of duties at close — but we hold **no** third-party attestation (SOC 2, GAAP audit) and must not imply one. | **H2-5** retention exempts control evidence; **H3-5** SoD + evidence; audit log | "Keeps an audit trail your accountant can follow — append-only, with separation of duties on the close." Never "compliant/compliance-ready/audit-ready" as a certification. |
| 4 | "Real-time books / real-time cash" | **Remove/Rewrite** | Statements and the cash outlook are computed on demand from posted facts; bank/QBO data arrives by periodic sync, not a live stream. | on-demand `computeTrialBalance`/statements; connector sync | "Up to date as of your last sync." Never "real-time / live / streaming." |
| 5 | "One-click GDPR/CCPA erasure" | **Remove/Rewrite (already fixed in-app)** | The erase action resets **owner-held web data only** and explicitly discloses what is retained (ledger, tax IDs, tokens, users, keys, billing, audit). It is not a full regulatory erasure. | **H2-3** honest-erase tests; `settings_screens.py` wording | "Reset your owner-held data. A full regulatory erasure is handled by support." (Live wording already matches.) |
| 6 | "Your system of record" | **Qualified** | True for the hardened v1 core — the ledger, the guided close, and the sealed package. The broader accounting surface is labelled **provisional** in-app and is not yet a system of record. | **H4-1** v1 scope + provisional labels; **H3-1…H3-8** accounting controls | "A system of record for your cash and your monthly close." Scope the claim to v1; don't imply the whole surface. |
| 7 | "Bank-grade / enterprise-grade security" | **Qualified** | Real hardening: per-tenant Postgres RLS, scoped single-use API keys, secrets + vendor TINs + webhook secrets encrypted at rest, Secure cookies + CSP nonces, SSRF-guarded webhooks, fail-closed auth. No third-party security certification is claimed. | **H1-3/5/6/7/8**, **H2-4**, **H2-6** and their tests | "Encrypted at rest, isolated per tenant, and access-scoped." Never "bank-grade/enterprise-grade" unless/until a certification exists. |
| 8 | "Never lose a transaction" | **Supported** | Append-only, reversal-only ledger; durable stores; a restore/migration drill in CI. Corrections are new entries, never edits. | append-only posting invariants; **H0** restore drill in CI | Keep, scoped to the ledger. |
| 9 | "Close your month with confidence" | **Supported** | The authoritative close gate refuses to seal books that don't tie out or whose controls fail; publish durably locks the period and seals an immutable, verifiable package; reopen needs a second person's approval. | **H2-1** publication gate; **H3-5** authoritative close + SoD | Keep. |
| 10 | "Multi-entity / multi-currency consolidation" | **Qualified** | Real consolidation with intercompany eliminations and ASC 830 translation (average-rate P&L, historical-rate equity, roll-forward CTA, per-line rate audit). Deep but not every edge (e.g. native FX transactions) is covered. | **H3-8** FX depth + consolidation tests | "Consolidate entities across currencies with a documented translation." Don't imply exhaustive coverage. |
| 11 | "Where's my cash?" (the core promise) | **Supported** | The 13-week outlook, breach detection, and scenario what-ifs, computed from posted facts with forecast-vs-fact provenance labels. | forecast engine tests; **H2-2** provenance | Keep — this is the product. |

## Standing rules

1. **No certification words** ("compliant", "audit-ready", "bank-grade", "SOC 2", "GAAP-audited") until a real attestation exists. We describe *mechanisms* (audit trail, encryption at rest, tenant isolation), not *certifications*.
2. **No "real-time".** Say "as of your last sync."
3. **Scope system-of-record claims to v1** (cash + close + package). The provisional surface is labelled as such in-app (H4-1) and must be labelled as such in copy.
4. **Automation is assistive.** We automate the repetitive and confirm the material; never "hands-off/fully automated."
5. Every new public claim gets a row here, with a passing gate, before it ships.

## Result

Every claim we intend to make publicly now maps to a passing gate (rows 1, 2, 6, 7, 8, 9, 10, 11) or has been rewritten to an honest form (rows 3, 4, 5). The three _Remove/Rewrite_ items are the ones to watch in any existing external copy; in-app wording for erasure (5) already matches the approved form.
