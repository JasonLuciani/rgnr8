# @rgnr8/ledger-postgres

PostgreSQL adapter for the RGNR8 accounting kernel. Implements the kernel's `LedgerStore` interface against a real database, so the same posting engine and invariants run on durable storage.

## What it provides

- **`PgLedgerStore implements LedgerStore`** — `append`, `getById`, `getBySequence`, `getByIdempotencyKey`, `list`, plus `migrate()`.
- **`CORE_DDL`** — tables, constraints, and indexes.
- **`RLS_DDL`** — optional row-level-security policies for tenant isolation.
- **`LEDGER_MIGRATIONS`** / **`LEDGER_MIGRATIONS_WITH_RLS`** — the schema as versioned, forward-only migrations for `@rgnr8/migrations` (ordered, checksum-tracked, applied once). The core set is pg-mem-safe (what the tests run); RLS is a second migration applied under a non-owner role in production. Prefer this over the ad-hoc `migrate()` for real deployments.

## Guarantees (matching the kernel contract)

- **Atomic, gap-free per-tenant sequencing.** A `ledger_tenant_seq` counter row is bumped inside the same transaction as the insert, so appends for a tenant serialize on it and sequence numbers are contiguous — no gaps, even under concurrency.
- **Idempotency.** `UNIQUE (tenant_id, idempotency_key)`. Replaying a key returns the existing entry. A concurrent duplicate that loses the race rolls back its own sequence bump (no gap) and returns the winning entry.
- **Append-only.** The adapter issues no `UPDATE`/`DELETE` against posted rows; corrections are new reversal entries (handled by the kernel engine).
- **Exact money.** Amounts are `NUMERIC(38,0)` minor units, rehydrated to the kernel's integer-based `Money`. No floating point.
- **Tenant isolation.** Every query filters by `tenant_id`; RLS is the second layer.

## Schema

`journal_entry` (one row per entry, with full provenance columns) and `journal_line` (one row per line, `amount_minor NUMERIC(38,0)`, `dimensions JSONB`), plus the `ledger_tenant_seq` counter. Primary key `(tenant_id, sequence)`; a `journal_line → journal_entry` foreign key; an index on `(tenant_id, account_id)` for balance queries.

## Usage

```ts
import { Pool } from "pg";
import { PgLedgerStore } from "@rgnr8/ledger-postgres";
import { PostingEngine, ChartOfAccounts, PeriodRegistry } from "@rgnr8/ledger-kernel";

const pool = new Pool({ connectionString: process.env.DATABASE_URL });
const store = new PgLedgerStore(pool);
// production: apply the versioned schema through the migration runner
import { runMigrations } from "@rgnr8/migrations";
import { LEDGER_MIGRATIONS } from "@rgnr8/ledger-postgres";
await runMigrations(pool, LEDGER_MIGRATIONS, { appliedAt: new Date().toISOString() });
// (or `await store.migrate()` as a dev convenience)

const engine = new PostingEngine(coa, store, new PeriodRegistry());
await engine.post(command, { postedAt: new Date().toISOString() });
```

### Enabling row-level security (production)

Run `RLS_DDL` and connect with a role that is **not** the table owner (owners bypass RLS). Set the tenant per transaction so policies apply:

```sql
SET app.tenant_id = 'acme';
```

The adapter already filters by `tenant_id`; RLS enforces it a second time at the database.

## Testing

Integration tests run real SQL through [`pg-mem`](https://github.com/oguimbal/pg-mem) — no external database needed:

```bash
npm test
```

Covered: append/read round-trip with exact money and provenance, contiguous per-tenant sequencing, idempotent replay (one row written), ordered `list`/`getBySequence`, cross-tenant isolation, and full posting-engine integration (trial balance ties, reversal nets to zero). pg-mem does not implement RLS, so tests exercise the adapter's explicit `tenant_id` filtering; validate `RLS_DDL` against a real PostgreSQL in staging.

## Notes / next

- `migrate()` runs idempotent `CREATE TABLE IF NOT EXISTS`; adopt a versioned migration tool (e.g. node-pg-migrate) before production.
- `sequence` is returned as a JS number; fine for realistic per-tenant volumes (well within `Number.MAX_SAFE_INTEGER`).
