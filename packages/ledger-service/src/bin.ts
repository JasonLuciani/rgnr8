/**
 * The runnable ledger service.
 *
 *   RGNR8_LEDGER_PORT      (default 8181)
 *   RGNR8_LEDGER_TOKEN     shared secret the web app presents (required in prod)
 *   DATABASE_URL           Postgres; when absent the service runs in-memory
 *                          (fine for local dev — nothing survives a restart)
 *   RGNR8_LEDGER_APP_URL   the least-privilege connection the service should
 *                          SERVE from, once migrations have run as the owner.
 *                          Strongly recommended: row-level security does not
 *                          apply to a superuser, so serving as one leaves tenant
 *                          isolation resting on application SQL alone.
 *
 * Provisioning that role, once, as the owner:
 *
 *   RGNR8_PROVISION_APP_ROLE=rgnr8_app \
 *   RGNR8_PROVISION_APP_PASSWORD=... \
 *   DATABASE_URL=postgres://owner@host/db npm run start -w @rgnr8/ledger-service
 *
 * The process creates the role, grants it row-level DML and nothing else, and
 * exits. Point RGNR8_LEDGER_APP_URL at it thereafter.
 */
import { createLedgerServer } from "./server.js";
import { LedgerService } from "./handlers.js";
import { InMemoryBackend, PostgresBackend, type LedgerBackend } from "./backend.js";
import { appRoleDdl, superuserWarning } from "./security.js";

async function main(): Promise<void> {
  const port = Number(process.env["RGNR8_LEDGER_PORT"] ?? 8181);
  const token = process.env["RGNR8_LEDGER_TOKEN"] ?? "";
  // Fail closed: refuse to serve with auth off unless a dev explicitly opts out.
  // Previously an empty token silently ran the ledger with NO auth at all.
  if (!token && process.env["RGNR8_LEDGER_ALLOW_NO_AUTH"] !== "1") {
    process.stderr.write(
      "RGNR8_LEDGER_TOKEN is required to serve the ledger. Set it, or set "
      + "RGNR8_LEDGER_ALLOW_NO_AUTH=1 for local dev only.\n",
    );
    process.exit(2);
  }
  const databaseUrl = process.env["DATABASE_URL"] ?? "";
  const appUrl = process.env["RGNR8_LEDGER_APP_URL"] ?? "";
  const provisionRole = process.env["RGNR8_PROVISION_APP_ROLE"] ?? "";

  let backend: LedgerBackend;
  if (databaseUrl) {
    const pg = await import("pg");
    // Migrations need owner rights; serving does not. When an app URL is given,
    // the two connections are deliberately different.
    const ownerPool = new pg.default.Pool({ connectionString: databaseUrl });
    await new PostgresBackend(ownerPool as never).migrate();

    if (provisionRole) {
      const password = process.env["RGNR8_PROVISION_APP_PASSWORD"] ?? "";
      if (!password) {
        process.stderr.write("RGNR8_PROVISION_APP_PASSWORD is required to provision a role\n");
        process.exit(2);
      }
      await ownerPool.query(appRoleDdl(provisionRole, password));
      process.stdout.write(
        `provisioned least-privilege role "${provisionRole}" — point `
        + "RGNR8_LEDGER_APP_URL at it and restart\n",
      );
      await ownerPool.end();
      return;
    }

    if (appUrl) {
      await ownerPool.end();
      const appPool = new pg.default.Pool({ connectionString: appUrl });
      backend = new PostgresBackend(appPool as never, { enforceRls: false });
      const warning = await superuserWarning(appPool as never);
      if (warning) process.stderr.write(`WARNING: ${warning}\n`);
    } else {
      backend = new PostgresBackend(ownerPool as never);
      const warning = await superuserWarning(ownerPool as never);
      if (warning) process.stderr.write(`WARNING: ${warning}\n`);
    }
  } else {
    backend = new InMemoryBackend();
    await backend.migrate();
  }

  const service = new LedgerService(backend, {
    ...(token ? { authToken: token } : {}),
    now: () => new Date().toISOString(),
  });

  createLedgerServer(service).listen(port, () => {
    process.stdout.write(
      `RGNR8 ledger service on :${port} — store=${databaseUrl ? "postgres" : "in-memory"}`
      + `, serving-as=${appUrl ? "app role" : "owner"}`
      + `, auth=${token ? "on" : "OFF (dev)"}\n`,
    );
  });
}

void main();
