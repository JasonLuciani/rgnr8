import type { Pool, PoolClient, QueryResult } from "./pgLedgerStore.js";

/**
 * Cross-store atomicity for multi-step operations (e.g. go-live: persist the
 * chart, post the opening entry, lock the period). Each Pg store normally opens
 * its own connection and BEGIN/COMMITs per call, so a sequence of store calls is
 * NOT one transaction. {@link runInTransaction} fixes that: it opens ONE
 * connection, BEGINs once, and hands the callback a `Pool` that all the stores
 * build over — so every write enlists in the same transaction and they commit or
 * roll back together.
 *
 * The trick is `SharedTxnPool`: it hands out the one shared client and suppresses
 * the stores' inner transaction-control statements (BEGIN/COMMIT/ROLLBACK/
 * SAVEPOINT), so only the outer runner controls the boundary. The tenant GUC the
 * stores set with `set_config(..., true)` is transaction-local and applies to the
 * outer transaction, so RLS still engages.
 */

function isTxnControl(sql: string): boolean {
  const s = sql.trim().toUpperCase();
  return (
    s === "BEGIN" ||
    s === "COMMIT" ||
    s.startsWith("ROLLBACK") ||
    s.startsWith("SAVEPOINT") ||
    s.startsWith("RELEASE")
  );
}

const EMPTY: QueryResult = { rows: [] };

class SharedTxnPool implements Pool {
  constructor(private readonly client: PoolClient) {}

  query(text: string, values?: unknown[]): Promise<QueryResult> {
    return isTxnControl(text) ? Promise.resolve(EMPTY) : this.client.query(text, values);
  }

  connect(): Promise<PoolClient> {
    const c = this.client;
    return Promise.resolve({
      query: (t: string, v?: unknown[]) =>
        isTxnControl(t) ? Promise.resolve(EMPTY) : c.query(t, v),
      release: () => {
        /* the shared client is released by the outer runner, not per-store */
      },
    });
  }
}

/**
 * Run `fn` inside a single database transaction. `fn` receives a `Pool` it MUST
 * build its stores over, so every write enlists in the one transaction. On any
 * throw the whole unit rolls back; otherwise it commits.
 */
export async function runInTransaction<T>(
  pool: Pool,
  fn: (txPool: Pool) => Promise<T>,
): Promise<T> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const result = await fn(new SharedTxnPool(client));
    await client.query("COMMIT");
    return result;
  } catch (err) {
    try {
      await client.query("ROLLBACK");
    } catch {
      /* connection may already be aborted */
    }
    throw err;
  } finally {
    client.release();
  }
}
