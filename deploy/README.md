# RGNR8 — deploy bundle

Infra-as-code for the Python side (owner web app + delivery worker). The
TypeScript ledger + financial-package migrations run via `@rgnr8/migrations`
separately (see `../DEPLOYMENT.md`).

## Files
- **Dockerfile** — serves `deploy/entry.py` under gunicorn; `/ready` health check baked in.
- **docker-compose.yml** — Postgres + a one-shot `migrate` + `web` + a `worker`.
- **Procfile** — `release` (migrate), `web` (gunicorn), `worker` (delivery tick) for PaaS.
- **.env.example** — every `RGNR8_*` setting, validated at boot by `Settings.from_env`.
- **entry.py** — `application = create_application(...)` WSGI callable.
- **migrate.py** — forward-only, drift-checked Python-owned schema (RLS on Postgres).
- **worker.py** — rehydrates the fleet, runs one delivery tick (idempotent).
- **db.py / _pathsetup.py** — connection opener + sys.path wiring (no install step).

## Local / UAT
```bash
cd deploy
cp .env.example .env         # set RGNR8_JWT_SECRET at minimum
docker compose up --build    # db → migrate → web (:8080) + worker
curl localhost:8080/ready    # {"status":"ready", ...}
```

## Production notes
- Terminate TLS at your load balancer; point its health check at `GET /ready`.
- For a real IdP set `RGNR8_AUTH_MODE=jwks` + issuer/audience/JWKS URL — tokens are verified against the published keys (RS256), no shared secret.
- Postgres enforces **per-tenant Row-Level Security** (migration v2); set the tenant GUC per request/connection with `rgnr8_ops.with_tenant(conn, tenant_id)` (or `SET rgnr8.tenant_id = '<tenant>'`).
- Run `worker.py` on a scheduler (K8s CronJob / cron), not as a loop, so a missed run never drops a week.
- Rotate `RGNR8_JWT_SECRET` / IdP keys through your secret store; nothing is baked into the image.
