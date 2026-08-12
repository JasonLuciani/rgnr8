# rgnr8-runtime

The delivery runtime — the long-running process that turns the weekly-briefing scheduler into something that actually runs. It ticks the scheduler on a real clock, and for every *due* subscription it runs the tenant's forecast, builds the briefing, validates it, and delivers it — with subscription state persisted so delivery is idempotent across restarts.

Python, depends on `rgnr8-forecast` + `rgnr8-briefing`. 8 tests, `mypy --strict` clean.

## The tick

`DeliveryRuntime.tick(now)` is the whole job in one deterministic step (`now` is injected — no system clock in the logic):

1. load every subscription (with its persisted `last_sent`) from the `SubscriptionStore`,
2. for each **due** subscription (per the scheduler's catch-up rule), resolve the tenant via a `TenantSource`, run its forecast, build the briefing, run the **unsupported-number validator**, and — only if it passes — build the delivery envelope,
3. hand the due set to the scheduler's `run_due`, which delivers and advances each fired subscription's `last_sent`,
4. persist every fired subscription back to the store.

A tenant whose numbers don't validate (or whose tenant record is missing) is **skipped** — its `last_sent` is not advanced, so it retries next tick rather than shipping an unbacked briefing. That's the same "never serve an unbacked number" invariant the web surface enforces.

## Durable subscription state

`last_sent` is the per-recipient cursor that makes delivery idempotent: the scheduler only fires when the most recent scheduled time is newer than it, so persisting `last_sent` is what stops a restart from re-sending. `SubscriptionStore` (`list` / `save`) has two implementations:

- **`InMemorySubscriptionStore`** — for tests / single-process dev.
- **`SqlSubscriptionStore`** — over any DB-API 2.0 connection (sqlite3 in tests, psycopg/Postgres in production), the same seam the web's `SqlTenantStore` uses. A test delivers, closes the connection, reopens it (simulating a restart), and confirms the persisted `last_sent` prevents a re-send this week but allows next week's.

Tenants are resolved through a `TenantSource` — `InMemoryTenantSource` (with `add_from_dto` to load from the `forecast-inputs/1` DTOs the TS side emits), production swaps a DB-backed source.

## Run it

```python
from rgnr8_runtime import DeliveryRuntime, SqlSubscriptionStore, InMemoryTenantSource, serve
from rgnr8_briefing import ProviderDeliverer  # + real EmailTransport/PushTransport

runtime = DeliveryRuntime(SqlSubscriptionStore(conn), tenant_source, ProviderDeliverer(email, push))
serve(runtime, interval_seconds=60)   # ticks forever; now/sleep are injectable for tests
```

## What's next

- A companion connector-sync runtime (periodic `ConnectorRunner.sync` over ACTIVE connections) for banks that don't emit webhooks.
- Load the `TenantSource` from the same DB the web app provisions from; add per-tenant delivery preferences.
- Wire a real `ProviderDeliverer` (SendGrid/SES/APNs/FCM) and a subscription-management surface.
