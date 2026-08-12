# rgnr8-ops

The **operator layer** — onboard beta clients the same way every time, and watch the whole fleet from one dashboard. This is what turns "the engines all work" into "I can run two clients through beta and see how they're doing."

Python, depends on `rgnr8-forecast` + `rgnr8-briefing` + `rgnr8-web` + `rgnr8-runtime`. 14 tests, `mypy --strict` clean.

## Onboarding (the `Fleet`)

Provisioning a client touches three subsystems — the web surface, the delivery runtime, and auth — and they have to agree. `Fleet.onboard(BetaTenant(...))` does it in one call: registers the client as a **web tenant**, a delivery **`Subscription`**, and a runtime **`TenantSource`** entry, all from the one inputs object.

```python
fleet = Fleet(jwt_secret=SECRET)
fleet.onboard(BetaTenant("acme", "Acme Co", "owner@acme.com", inputs, config))
app = fleet.web_app(packages=package_reader)   # JWT-authed, serves every onboarded client
token = fleet.mint_token("acme")               # a signed session JWT (as an IdP would issue)
runtime = fleet.delivery_runtime(deliverer)    # weekly briefings over the fleet's subscriptions
```

`mint_token` hands out a real HS256 session JWT; a token for one client can't reach another's data (enforced by the web app, covered by a test).

### Onboarding straight from a `forecast-inputs/1` DTO

`Fleet.onboard_from_dto(tenant_id, name, recipient, dto, minimum_cash, schedule=?)` is the seam **both** onboarding stages land on. The QBO **overlay** (RGNR8 on top of QBO — bank balances + open AR/AP) and the full **migration** (QBO GeneralLedger imported into the RGNR8 ledger, then reconciled) both emit the same `forecast-inputs/1` payload, so onboarding doesn't care which path a client arrived by — it runs `from_dto` and provisions the client everywhere. The DTO (dict or JSON text) carries the cash facts; `minimum_cash` is the owner's floor (operator-set, not in the DTO). See `packages/prototype/onboard.py` for both stages onboarded into one fleet.

## The fleet dashboard (`build_ops_report` / `render_ops_html`)

`build_ops_report(fleet, now)` (deterministic — `now` injected) runs each client's forecast, reads the briefing status (STABLE / WATCH / AT_RISK), and pulls the delivery cursor, producing a row per client with: **cash today, floor, cash trough + date, breach timing + shortfall, this-week delivery state, and next-due**. Rows are ranked **worst-first** (AT_RISK, then soonest breach), and the report rolls up how many clients are at risk and how many briefings have gone out this week. `render_ops_html` draws the self-contained dashboard.

So an operator opens one page and immediately sees: which client's cash is at risk, how soon they breach, and whether their briefing has been sent.

## Books-current + close-progress (operational status)

Cash is only half the picture — the other half is whether it can be *trusted*: are the connectors still feeding fresh data, and has the month-end close been done? `Fleet.set_status(tenant_id, TenantOpsStatus(connectors=…, close=…))` attaches per-client operational status, and the dashboard grows two columns — **Books** (connector health) and **Close** (close progress) — with a green/red dot each. `ConnectorHealth` mirrors `@rgnr8/connectors buildHealthReport`'s headline counts (total / needs-attention / stale → `books_current`); `CloseProgress` mirrors `@rgnr8/close closeCalendarStatus` (period, done/total, overdue → `complete`). Those two engines live in the TypeScript half, so in production the operator serializes their reports into these small injected Python objects — which keeps `build_ops_report` deterministic. The banner now flags *books behind* even when cash is fine, and the header rolls up `closed / total`. A client with no status attached renders as `—` (never guessed).

## What's next

- Persist the fleet (DB-backed `TenantStore`/`SubscriptionStore`) so it survives restarts.
- A thin serializer on the TS side that emits the `ConnectorHealth`/`CloseProgress` JSON the operator feeds to `set_status`, closing the loop from the live connector runtime + close calendar to the dashboard.
