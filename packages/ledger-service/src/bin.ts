/**
 * The runnable ledger service.
 *
 *   RGNR8_LEDGER_PORT      (default 8181)
 *   RGNR8_LEDGER_TOKEN     shared secret the web app presents (required in prod)
 *   DATABASE_URL           Postgres; when absent the service runs in-memory
 *                          (fine for local dev — nothing survives a restart)
 */
import { createLedgerServer } from "./server.js";
import { LedgerService } from "./handlers.js";
import { InMemoryBackend, PostgresBackend, type LedgerBackend } from "./backend.js";

async function main(): Promise<void> {
  const port = Number(process.env["RGNR8_LEDGER_PORT"] ?? 8181);
  const token = process.env["RGNR8_LEDGER_TOKEN"] ?? "";
  const databaseUrl = process.env["DATABASE_URL"] ?? "";

  let backend: LedgerBackend;
  if (databaseUrl) {
    const pg = await import("pg");
    const pool = new pg.default.Pool({ connectionString: databaseUrl });
    backend = new PostgresBackend(pool as never);
  } else {
    backend = new InMemoryBackend();
  }
  await backend.migrate();

  const service = new LedgerService(backend, {
    ...(token ? { authToken: token } : {}),
    now: () => new Date().toISOString(),
  });

  createLedgerServer(service).listen(port, () => {
    process.stdout.write(
      `RGNR8 ledger service on :${port} — store=${databaseUrl ? "postgres" : "in-memory"}, auth=${token ? "on" : "OFF (dev)"}\n`,
    );
  });
}

void main();
