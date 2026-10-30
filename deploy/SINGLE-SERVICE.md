# Single-service mode: web + ledger in one container

**Status: opt-in.** The default deploy is still two Render services, `rgnr8-web`
(deploy/Dockerfile) and the private `rgnr8-ledger` (deploy/Dockerfile.ledger).
Single-service mode runs both processes in the `rgnr8-web` container and lets the
separate `rgnr8-ledger` service ($7/mo Starter) be suspended and later deleted.
Switching back is a settings change; nothing in the database changes either way.

## How it works

`deploy/Dockerfile.combined` is the Python web image plus the ledger exactly as
`Dockerfile.ledger` builds it (under `/app/ledger`) and a Node runtime. Its command
is `python combined_launcher.py`, a small stdlib-only supervisor:

1. Starts the ledger (`node /app/ledger/packages/ledger-service/dist/bin.js`) bound
   to **127.0.0.1:8181** only (`RGNR8_LEDGER_HOST`), with `DATABASE_URL` defaulted
   from `RGNR8_DATABASE_URL`. The ledger runs its own schema migration on boot
   (serialized by an advisory lock), as it does today.
2. Waits for the ledger's `/health`. If the ledger exits or is not healthy within
   `RGNR8_LEDGER_BOOT_TIMEOUT` seconds (default 90), it logs `[combined] FATAL ...`
   and exits 1 **without starting gunicorn**, so the new deploy fails its `/ready`
   health check and Render keeps the previous instance serving.
3. Starts `gunicorn combined_entry:application --workers 2 --bind 0.0.0.0:$RGNR8_PORT`
   (same as today's Docker Command) with `RGNR8_LEDGER_URL=http://127.0.0.1:8181`
   forced, so a leftover value from the two-service setup cannot point the books
   screens at the old ledger. Both processes get the same `RGNR8_LEDGER_TOKEN`.
4. Supervises: if either process dies, the other is stopped and the container exits
   non-zero, so Render restarts the whole thing. SIGTERM is forwarded to gunicorn
   first (in-flight requests finish against a live ledger), then the ledger.

`/ready` keeps its contract (always 200 once serving, never a 503) and reports
`ledger.reachable` and `ledger_skew`. In this mode gunicorn is not even listening
until the ledger is healthy, and both processes come from one build, so skew
cannot happen.

Settings the launcher reads (all optional except the token and a database URL):

| Variable | Default | Meaning |
|---|---|---|
| `RGNR8_LEDGER_TOKEN` | required | Shared web↔ledger secret. Any non-empty value works; it never leaves the container. |
| `RGNR8_DATABASE_URL` / `DATABASE_URL` | required (one of) | The ledger uses `DATABASE_URL`, else `RGNR8_DATABASE_URL`. Same database as today. |
| `RGNR8_PORT` / `PORT` | `10000` | Public gunicorn port. |
| `RGNR8_WORKERS` | `2` | Gunicorn workers. |
| `RGNR8_LEDGER_INTERNAL_PORT` | `8181` | Loopback port for the ledger. |
| `RGNR8_LEDGER_BOOT_TIMEOUT` | `90` | Seconds to wait for the ledger's `/health`. |
| `RGNR8_SHUTDOWN_GRACE` | `25` | Seconds each child gets after SIGTERM before SIGKILL. |

Extra arguments to `combined_launcher.py` are appended to the gunicorn command
(e.g. `python combined_launcher.py --max-requests 20000 --max-requests-jitter 2000`).

## Memory (Starter, 512 MB)

Measured locally with the same process layout (launcher + ledger + gunicorn master
+ 2 workers; Python 3.13 / Node 22 here, the image uses 3.11 / 20), after ~6k
paced ledger-backed page loads plus ~60k mixed requests:

| Process | PSS |
|---|---|
| launcher | ~10 MB |
| ledger (node) | ~70 MB at boot, plateau ~82 MB |
| gunicorn master | ~15 MB |
| 2 workers | ~68 MB each |
| **total** | **~226 MB at boot, ~243 MB steady** (RSS sum ~298 MB, which double-counts shared pages) |

Render reported `rgnr8-web` at ~187 MB fresh, so expect roughly 270-290 MB on
Render in this mode: well inside 512 MB, with the worker-growth fix
(bounded observability sinks) keeping the web side flat.

## Try it locally

```bash
cd deploy
docker compose --profile combined up --build db migrate web-combined
curl localhost:8090/ready      # "ledger": {"reachable": true, ...}, "ledger_skew": null
```

CI builds and boots this image too (`combined-image-smoke`), checks `/ready`,
checks the ledger is not reachable off-loopback, and kills the ledger to prove the
container exits.

## Switching rgnr8-web over on Render (dashboard)

Do this in the dashboard on **rgnr8-web**, in order:

1. **Read the Blueprint note below first.** If the Blueprint auto-syncs, a sync
   puts these settings back to render.yaml's two-service values.
2. *Settings → Build & Deploy*: **Dockerfile Path** → `./deploy/Dockerfile.combined`.
3. *Settings → Build & Deploy*: **Docker Command** → `python combined_launcher.py`.
   (Render's Docker Command replaces the image's command, so the old gunicorn
   command must go or the ledger never starts.) Leave the Pre-Deploy Command
   (`python migrate.py`) and Health Check Path (`/ready`) as they are.
4. *Environment*:
   - `RGNR8_LEDGER_TOKEN`: make sure it has a plain value. Copying rgnr8-ledger's
     current value is simplest; any new random value also works.
   - `DATABASE_URL`: optional (the launcher falls back to `RGNR8_DATABASE_URL`). If
     you add it, use rgnr8-db's **internal** connection string, the same value as
     `RGNR8_DATABASE_URL`.
   - `RGNR8_LEDGER_URL`: delete it, or set it to `http://127.0.0.1:8181`. If left
     pointing at rgnr8-ledger it is ignored (the launcher logs that it overrode it).
5. **Save and deploy** (Manual Deploy → Deploy latest commit, or save with deploy).
   In the deploy log look for
   `[combined] ledger healthy; starting web: gunicorn ...`.
6. **Verify:** `https://acctg.rgnr8ventures.com/ready` shows
   `"ledger": {"reachable": true, ...}` and `"ledger_skew": null`. Sign in and open
   the books screens (Ledger → Accounts, Trial balance, General ledger, a register)
   and one invoice/bill list; they should match what they showed before. Watch
   rgnr8-web's memory graph for a day.
7. **Suspend rgnr8-ledger** (its page → Suspend). Nothing should change: re-check
   step 6. Suspended services are not billed. `rgnr8-worker` also carries
   `RGNR8_LEDGER_URL`/`RGNR8_LEDGER_TOKEN` from rgnr8-ledger, but `worker.py` does
   not call the ledger, so the cron is unaffected.
8. **Delete rgnr8-ledger later**, after a week or so, and only once render.yaml no
   longer lists it (next section). While render.yaml still defines it, a Blueprint
   sync expects it to exist.

**Rollback:** resume rgnr8-ledger, set rgnr8-web back to Dockerfile Path
`./deploy/Dockerfile`, Docker Command
`gunicorn combined_entry:application --workers 2 --bind 0.0.0.0:10000 --access-logfile -`,
and `RGNR8_LEDGER_URL` back to rgnr8-ledger's internal `host:port`; deploy.
(A Blueprint sync does exactly this, from render.yaml.)

## Blueprint (render.yaml) interaction

render.yaml is deliberately **unchanged** by this mode (comments only), so merging
it changes nothing on Render:

- **Sync with the file as it is** re-applies rgnr8-web's two-service settings:
  `dockerfilePath: ./deploy/Dockerfile`, the gunicorn `dockerCommand`, and
  `RGNR8_LEDGER_URL`/`RGNR8_LEDGER_TOKEN` from rgnr8-ledger. That is the rollback
  above. If rgnr8-ledger is suspended at that point, the books screens lose their
  ledger until it is resumed, so pause auto-sync (or don't sync) while running in
  single-service mode with the file unchanged.
- **Deleting rgnr8-ledger while render.yaml still lists it**: a later sync would
  try to create it again (as far as we know), with a new generated token.
- **Removing a service from render.yaml does not delete it.** Render does not
  delete resources that disappear from a Blueprint; deletion is a dashboard/API
  action. So the safe end state is: switch (steps above) → verify → apply the diff
  below → sync → delete rgnr8-ledger in the dashboard.

The follow-up render.yaml change, once single-service mode is proven (not applied
here; it also needs `packages/ops/tests/test_render_blueprint.py` updated):

```yaml
  # rgnr8-web
    dockerfilePath: ./deploy/Dockerfile.combined
    dockerCommand: python combined_launcher.py
    envVars:
      # remove the RGNR8_LEDGER_URL fromService entry (the launcher sets it)
      - key: RGNR8_LEDGER_TOKEN       # was fromService rgnr8-ledger
        generateValue: true           # or sync: false and paste the current value
  # rgnr8-worker: remove RGNR8_LEDGER_URL / RGNR8_LEDGER_TOKEN (unused by worker.py)
  # rgnr8-ledger: remove the whole pserv block, then delete the service in the dashboard
```

With that applied, a sync updates rgnr8-web and rgnr8-worker in place (no
recreate; names are unchanged) and leaves the rgnr8-ledger service alone.

## Risks and limits

- **One failure domain.** A ledger crash now restarts the web app too (by design:
  better than a web app with no books behind it). Restarts take a few seconds.
- **Ledger serves as the database owner**, same as today's rgnr8-ledger service
  (no `RGNR8_LEDGER_APP_URL`); its row-level-security warning still applies.
- **Shell-wrapped Docker Command.** If Render ran the command under a shell that
  does not forward SIGTERM, shutdown would fall back to SIGKILL after Render's
  grace period. That loses in-flight requests only; Postgres transactions roll back.
- **During the first deploy** the old rgnr8-ledger and the new in-container ledger
  briefly share the database. Both are the same build; migrations are idempotent
  and lock-serialized.
