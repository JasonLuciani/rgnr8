# @rgnr8/prototype — the end-to-end demo

One runnable harness that drives the **whole RGNR8 loop across both languages** on a demo business ("Bright Agency"), and writes the owner-facing pages to `./out` for viewing.

```bash
cd packages/prototype
python run.py        # runs the TS seed too, then the Python owner loop
python onboard.py    # the two-stage onboarding proof (overlay + migration)
```

## Onboarding proof (`onboard.ts` + `onboard.py`)

A second, focused harness proving the **two-stage onboarding layer** end to end across both languages. `onboard.ts` runs **Stage 1 (overlay)** — a live QBO snapshot (bank balances + open AR/AP) mapped to a `forecast-inputs/1` DTO, QBO staying the system of record — and **Stage 2 (migration)** — the QBO GeneralLedger imported into the RGNR8 ledger, verified against QBO's reported trial balance (parallel close), opening cash derived from RGNR8's own posted ledger. Both stages emit the *same* contract; the demo shows them tying to the penny (`63,210.55` from QBO's reported balance and from RGNR8's posted ledger). `onboard.py` then onboards **both** clients into one `Fleet` via the single `onboard_from_dto` path, runs each forecast, authorizes each owner's web route, and renders the operator fleet dashboard. Writes `out/overlay_inputs.json`, `out/migration_inputs.json`, `out/migration_diff.html`, `out/onboard_summary.json`, and `out/onboard_fleet.html`.

## What it does

1. **TypeScript accounting stack** (`seed.ts`, run via `npm run seed`): takes a sample QuickBooks CSV export (chart of accounts + journal + trial balance), **imports it into the ledger**, computes statements, runs the **parallel-close diff** against QBO (ties to the penny), passes the **month-end close gate**, and **seals an immutable, fingerprinted financial package**. Writes `out/package.json`, `out/seed_summary.json`, and the two QBO-diff HTML pages.
2. **Python owner experience** (`run.py`): reads the sealed package back and **re-verifies its SHA-256 fingerprint** (the cross-language integrity check — TS sealed it, Python independently vouches for it); builds the **13-week cash forecast**, the **validated weekly briefing**, and the interactive **Today dashboard**; boots the **web app** with **JWT session auth** + the package reader and exercises every owner route in-process (including blocking a valid-signature token for an unregistered tenant); and runs the **delivery runtime** one tick through the **real HTTP email transport** (fake client) to prove a briefing would be sent — then re-ticks to show idempotency.

## The artifacts (`./out`)

- `today.html` — the interactive owner "Today" dashboard (cash chart, status, recommended action).
- `package.html` — the published financial package for the closed period, served by the web app with a ✓-verified badge.
- `qbo_trial_diff.html` / `qbo_statements_diff.html` — RGNR8 vs QuickBooks, account-by-account and at the statement level.
- `briefing.txt` — the weekly owner briefing as delivered.

## The path it proves

```
QBO CSV → ledger → close → sealed package        (TypeScript)
   → re-verified in Python → forecast → briefing → Today dashboard
   → served over JWT-authed web → delivered via HTTP email transport   (Python)
```

Every hop is real code exercised by the platform's 300+ tests; this harness just wires them into a single end-to-end run you can watch and open.
