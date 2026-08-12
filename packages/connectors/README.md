# @rgnr8/connectors

Live incremental sync from bank/payroll providers into the ingestion pipeline. Handles the operational reality of real connectors — cursor pagination, token health, and rate-limit retry/backoff — over a **pluggable HTTP transport**, so going live is just providing a fetch-backed client and real credentials.

TypeScript, depends on `@rgnr8/ingestion`. 22 tests, `tsc --strict` clean.

## The transport seam

`HttpClient.request(HttpRequest) -> HttpResponse` is the only thing to implement for production (wrap `fetch`). Tests use `FakeHttpClient`, a scripted transport, so the entire sync/retry/health state machine is exercised deterministically with no network.

## Connectors

- **`PlaidConnector`** — uses Plaid's incremental `/transactions/sync`. The connection's `cursor` advances each page; `added`/`modified` become RawRecords, `removed` are counted as tombstones. Maps Plaid error codes to typed errors (`ITEM_LOGIN_REQUIRED` → expired, `INVALID_ACCESS_TOKEN`/`ITEM_NOT_FOUND` → revoked, 429 → rate-limited).
- **`GustoConnector`** — fetches processed payroll runs and maps each to a RawRecord shaped for the ingestion Gusto adapter (net pay + taxes + settlement date).

## The runner

`ConnectorRunner.sync(connectionId, fetchedAt)`:

- **Paginates** via the connector's cursor until `has_more` is false, bounded by `maxPages` (a run can't loop forever — over the cap it returns `truncated: true`).
- **Retries rate limits** with backoff up to `maxRetries` (the sleep is injected, so tests are instant and deterministic); exhaustion ends in `ERROR`.
- **Updates connection health** — `ACTIVE` on success (with `lastSyncedAt` + advanced cursor persisted), `EXPIRED`/`REVOKED` on auth failures. A `REVOKED` connection is skipped on the next run without calling the API (it needs reconnect).
- Returns the `RawRecord[]` to hand straight to `IngestionPipeline.ingest(...)`.

The integration test proves the whole path: `ConnectorRunner.sync` → RawRecords → `IngestionPipeline` → canonical transactions (sign-normalized), and re-syncing the same page is idempotent.

## Going live

`FetchHttp` is the production transport — it wraps `fetch` (injectable for tests), serializes JSON bodies, and parses responses defensively (a non-JSON/empty body becomes `{}` so the connector's own error mapping decides the outcome, not a parse crash).

```ts
import { FetchHttp, ConnectorRunner, PlaidConnector, GustoConnector } from "@rgnr8/connectors";
const runner = new ConnectorRunner(store, new FetchHttp(), [new PlaidConnector(), new GustoConnector()]);
const report = await runner.sync(connectionId, fetchedAtIso);
pipeline.ingest(report.rawRecords);
```

## Webhook-driven sync

`handlePlaidWebhook(store, payload)` turns a raw Plaid webhook into a decision without doing any HTTP itself (so it's pure and testable) — the caller owns actually running `ConnectorRunner.sync` when told to:

- `TRANSACTIONS` / `SYNC_UPDATES_AVAILABLE` (and `DEFAULT`/`INITIAL`/`HISTORICAL_UPDATE`) → **`sync`** — pull the new data now instead of polling.
- `ITEM` / `ERROR` with `ITEM_LOGIN_REQUIRED` → marks the connection **EXPIRED**, action **`reconnect`**; `INVALID_ACCESS_TOKEN`/`ITEM_NOT_FOUND` → **REVOKED**.
- `ITEM` / `PENDING_EXPIRATION` → **`reconnect_soon`** (nudge the owner before consent lapses).
- `ITEM` / `USER_PERMISSION_REVOKED` → **REVOKED** + `reconnect`.
- An `item_id` that matches no connection → **`unknown_item`**, nothing changed.

Connections are resolved by matching `secrets.item_id` to the payload's `item_id` (override with your own `ConnectionResolver`).

## Connection-health dashboard

`buildHealthReport(store, now)` (deterministic — `now` injected) buckets every connection by health, flags the ones that **need reconnecting** (EXPIRED/REVOKED/ERROR) and the ones that have gone **stale** (active but not synced within `staleAfterHours`, default 26h — or never synced). `renderHealthHtml(report)` produces a self-contained dashboard (validated dataviz palette) an operator or the owner can open.

## Periodic sync runtime

`SyncRuntime` is the loop for connections that can't rely on webhooks — it pulls on a cadence. `tick(nowMs, fetchedAt)` (clock injected, so it's deterministic) walks every connection and:

- runs `ConnectorRunner.sync` on the ones that are **due** (never-synced → immediately; otherwise `lastSyncedAt + intervalMs`), handing each success's RawRecords to a **sink** (the ingestion pipeline);
- **backs off** a connection that syncs to `ERROR` — the next attempt is `now + backoffBaseMs·2^(failures-1)` (capped), and it retries only once that elapses; a success resets the backoff;
- **skips** `REVOKED`/`EXPIRED` connections entirely — they need the owner to reconnect, so the runtime never auto-hits the API for them (a test asserts zero requests).

`serveSync(runtime, { intervalMs, now, sleep, maxTicks })` is the thin real-clock loop around `tick` (now/sleep injectable, `maxTicks` for tests). This is the inbound twin of `rgnr8-runtime`'s outbound delivery loop.

## What's next

- OAuth token-refresh hooks; persist the runtime's backoff/schedule state (today it's in-process).
- More providers (additional banks, cards, other payroll systems) behind the same `Connector` contract.
