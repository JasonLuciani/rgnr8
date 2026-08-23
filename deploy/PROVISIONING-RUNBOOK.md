# RGNR8 Provisioning Runbook

_The step-by-step for standing up a hosted RGNR8 that can safely hold a real client's books. Everything the code needs is built; the steps below are the ones that require your cloud accounts and secrets, in the order to do them._

This runbook covers the operational gate identified in the executive review: managed Postgres, TLS, a domain, backups, encryption at rest, error tracking, and the delivery worker. Work top to bottom; each step says what to do and how to verify it before moving on.

## 0. Prerequisites

You will need: a cloud account that offers managed PostgreSQL and a container/app host (Render, Fly.io, Railway, AWS, GCP — the manifests in this folder are host-agnostic Docker), a domain you control, and a place to store secrets (your host's secret manager or a vault). The build ships a `Dockerfile`, `docker-compose.yml`, and `Procfile` in this directory. For Render specifically, the repo root carries a reviewed **`render.yaml`** Blueprint that implements this runbook's topology (public web at the custom domain, PRIVATE ledger + operator, cron worker, managed Postgres 16, `RGNR8_REQUIRE_RLS=1`) — create a Blueprint instance from it and you land at step 1 with most of steps 2–4 pre-wired.

## 1. Managed PostgreSQL 16

Create a managed Postgres 16 instance. Managed (not self-run) is the point: you want automated failover and point-in-time recovery you didn't have to build. Capture its connection string as `DATABASE_URL` / `RGNR8_DATABASE_URL`.

Then create the least-privilege application role — the service must **not** connect as the database owner, or row-level security is silently bypassed (the code checks for this at boot and warns). Run once, as the owner:

```
RGNR8_PROVISION_APP_ROLE=rgnr8_app \
RGNR8_PROVISION_APP_PASSWORD='<a long random password>' \
DATABASE_URL=postgres://OWNER@HOST/db \
  npm run start -w @rgnr8/ledger-service      # provisions the role, then serves
```

Verify: connect as `rgnr8_app` and confirm `SELECT * FROM journal_entry` with no tenant set returns **zero rows** (RLS fails closed). The real-Postgres test suite asserts exactly this.

## 2. Encryption key for secrets at rest

Generate a Fernet key and store it as `RGNR8_SECRET_KEY` in your secret manager:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

With it set, stored QBO OAuth tokens are encrypted at rest; without it they are plaintext (dev only). **Back this key up separately from the database** — losing it means stored tokens can't be decrypted and each client must reconnect QBO. If you set it after tokens already exist, run the one-shot migration `SqlConnectionStore.reencrypt_all()` to encrypt the existing rows.

Verify: after connecting a QBO account, inspect the `rgnr8_qbo_connection` row — the `access_token`/`refresh_token` in the JSON must be prefixed `enc:fernet:v1:`, not the raw token.

## 3. Schema migration (release phase)

Two migration steps, both idempotent, run on every deploy before traffic:

- **Python-owned tables** (web state, users, audit, billing, QBO connections, webhook endpoints + outbox): `python deploy/migrate.py` (this is the compose `migrate` service).
- **TypeScript ledger + financial-package tables**: `@rgnr8/migrations` (see `DEPLOYMENT.md`). The ledger service also self-migrates on boot under a Postgres advisory lock, so two instances starting at once won't race.

Verify: hit `/ready` on the ledger service and `/ready` on the web app — both must return `200`.

## 4. TLS and the domain

Point your domain at the web host and terminate TLS at the host's load balancer / ingress (managed certificates are simplest). RGNR8 speaks plain HTTP behind the proxy; the proxy adds HTTPS. Set the public URL as needed for OAuth redirect URIs (step 6).

Verify: `https://your-domain/ready` returns `200` over a valid certificate; `http://` redirects or is closed.

## 5. Backups with a tested restore

Schedule `deploy/backup.sh` daily against `DATABASE_URL` (cron/K8s CronJob):

```
15 2 * * *  DATABASE_URL=... /opt/rgnr8/deploy/backup.sh /var/backups/rgnr8
```

Then — this is the part most teams skip — **test the restore on a schedule**. Weekly, restore the newest dump into a scratch database and boot the app against it:

```
TARGET_DATABASE_URL=postgres://.../scratch  deploy/restore.sh --latest /var/backups/rgnr8
RGNR8_DATABASE_URL=postgres://.../scratch   # point a throwaway app at it, hit /ready, sign in
```

Verify: the restore completes, `/ready` is green against the restored database, and the books open. An untested backup is a guess.

## 6. QBO production credentials

In the Intuit developer portal, create a **production** app, set the redirect URI to `https://your-domain/oauth/qbo/callback`, and set:

```
RGNR8_QBO_CLIENT_ID=...
RGNR8_QBO_CLIENT_SECRET=...
QBO_ENVIRONMENT=production
```

The OAuth connect flow must be done with the client present (they authorize at Intuit); RGNR8 never sees their Intuit password. Tokens land encrypted (step 2).

Verify: a test connection completes, and `rgnr8_qbo_connection` shows `status=connected` with encrypted tokens.

## 7. Error tracking

Set `SENTRY_DSN` to your Sentry project's DSN. Unhandled errors are then reported with a redacted context and a reference id (surfaced to the client in the 500 body); without a DSN they are logged as structured JSON. Nothing is ever silently dropped.

Verify: trigger a deliberate error in staging and confirm it appears in Sentry with a reference id that matches the `ref` in the 500 response.

## 8. The delivery worker

Run `deploy/worker.py` on a schedule (cron/CronJob/timer, not a long-lived loop) — it delivers due briefings and drives the durable webhook outbox (retries with backoff, dead-letters after exhaustion, replayable from the Integrations screen). A missed tick never drops work; it just delivers on the next run.

Verify: register a webhook endpoint, seal a close, run the worker, and confirm the delivery moves from `pending` to `delivered` in the outbox (or retries/dead-letters if the endpoint is down).

## 9. Beta cutover checklist

- [ ] `rgnr8_app` least-privilege role in use (not the DB owner); RLS proven fail-closed.
- [ ] `RGNR8_SECRET_KEY` set and backed up separately; QBO tokens encrypted at rest.
- [ ] Both `/ready` probes green; migrations run in the release phase.
- [ ] TLS valid on the domain; OAuth redirect URI matches.
- [ ] Daily backups running **and** a restore tested this week.
- [ ] `SENTRY_DSN` set; a test error observed end to end.
- [ ] Delivery worker scheduled; a webhook delivered end to end.
- [ ] `RGNR8_AUTH_MODE` set for your IdP (or `hs256` with a long random `RGNR8_JWT_SECRET`).

When every box is checked, the operational gate from the executive review is closed and a founder-supervised design-partner beta can hold real data.
