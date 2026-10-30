# Runbook: move `rgnr8-db` to a right-sized database

**Why.** `rgnr8-db` holds about 100 MB on a 15 GB disk. Render bills storage per GB and
can't shrink a disk, so the 15 GB costs about $4.50/month forever. A new database with
a 1 GB disk on the same compute plan (`0.1c-256mb`) costs about $6.20/month all in,
versus about $10.30 today. That saves roughly **$4/month**.

**Downtime.** About 5 minutes, while the data is copied. The app shows Render's
maintenance page, and no accounting writes happen during the copy.

**Constraints that shape the steps:**

- `rgnr8-db` is **Blueprint-managed**. Every `fromDatabase: rgnr8-db` reference in
  `render.yaml` gets re-applied on sync. Repointing URLs by hand in the dashboard
  would be undone, so the switch happens through `render.yaml`, in two commits.
- `rgnr8-db` accepts **private-network traffic only** (inbound rules emptied
  2026-10-28). The copy must therefore run *inside* Render. The app image
  (`python:3.11-slim`) has no `pg_dump`, so the copy runs as a throwaway cron job
  from the official `postgres:16` image.
- Do this **after** the single-service web+ledger change, if that lands first. Both
  edit `render.yaml`, and each should be a clean diff.

---

## Phase 1: create the new, empty database (no downtime)

1. Add this block under `databases:` in `render.yaml`. Leave every existing
   `fromDatabase: rgnr8-db` reference alone for now.

   ```yaml
     - name: rgnr8-db-v2
       plan: 0.1c-256mb
       diskSizeGB: 1
       region: oregon
       postgresMajorVersion: "16"
       ipAllowList: []        # private network only, same as rgnr8-db today
   ```

2. Merge. The Blueprint sync creates `rgnr8-db-v2`, which nothing uses yet.
3. Confirm in the dashboard that `rgnr8-db-v2` shows **Available**, 1 GB disk, and
   "All internet traffic is blocked".

## Phase 2: copy the data (≈5 min of maintenance)

1. **Stop writes.**
   - `rgnr8-web`: Settings → Maintenance Mode → **On**.
   - `rgnr8-worker` (cron): **Suspend**. Its next 15-minute run must not land
     mid-copy.
2. **Create the copy job.** New → Cron Job → **Deploy an existing image** →
   `docker.io/library/postgres:16`. Settings:
   - Name `rgnr8-db-copy`, region **Oregon**, smallest plan.
   - Schedule `0 0 1 1 *`. You'll trigger it by hand; this schedule just keeps it
     from firing on its own.
   - Environment:
     - `OLD_URL` = `rgnr8-db` → Connect → **Internal** Database URL
     - `NEW_URL` = `rgnr8-db-v2` → Connect → **Internal** Database URL
   - Command:

     ```sh
     sh -c 'set -e
       pg_dump "$OLD_URL" -Fc --no-owner --no-privileges -f /tmp/rgnr8.dump
       pg_restore --list /tmp/rgnr8.dump >/dev/null
       pg_restore --no-owner --no-privileges --exit-on-error -d "$NEW_URL" /tmp/rgnr8.dump
       for t in $(psql "$OLD_URL" -Atc "select quote_ident(tablename) from pg_tables where schemaname = current_schema() order by 1"); do
         a=$(psql "$OLD_URL" -Atc "select count(*) from $t"); b=$(psql "$NEW_URL" -Atc "select count(*) from $t")
         echo "$t old=$a new=$b"; [ "$a" = "$b" ] || { echo "MISMATCH $t"; exit 1; }
       done
       echo COPY-OK'
     ```

     All production tables live in `public`, which is `current_schema()` for the
     Render user. Only tests create other schemas. Row-level security policies come
     across in the dump's post-data section, after the rows are loaded, so the restore
     isn't blocked by them.
3. **Trigger Run**, then read the job's logs. Expect one `old=N new=N` line per table
   and a final **`COPY-OK`**. If there's any `MISMATCH` or error, stop here. Turn
   Maintenance Mode off and resume the worker. Nothing has been repointed, so the app
   is exactly as before.

## Phase 3: switch the app to the new database (still in maintenance)

1. In `render.yaml`, change every `fromDatabase: name: rgnr8-db` to
   `name: rgnr8-db-v2`. That covers `rgnr8-web` (`RGNR8_DATABASE_URL`), the ledger
   (`DATABASE_URL`, on `rgnr8-ledger` or the combined web service) and `rgnr8-worker`
   (`RGNR8_DATABASE_URL`). Keep the old `rgnr8-db` block for now.
2. Merge. The sync updates the env vars and the services redeploy, and `rgnr8-web`
   runs `python migrate.py` against the new database. It should be a no-op, because
   the schema came over in the dump.
3. **Verify, then reopen.**
   - `https://acctg.rgnr8ventures.com/ready` returns `"status": "ready"`.
   - Maintenance Mode **Off**.
   - Sign in, open the books and one ledger-backed screen, and check that a recent
     transaction you know is there.
   - **Resume** `rgnr8-worker`, and check its next run's log for errors.
4. Delete the `rgnr8-db-copy` cron job.

**Rollback**, any time before Phase 4: change the references back to `rgnr8-db` and
merge. The old database was never written to after the copy. Anything entered after
the switch would only exist in `rgnr8-db-v2`, so export it first if needed.

## Phase 4: retire the old database (only on Jason's explicit go-ahead)

Wait about a week of normal use first.

1. **Final safety copy.** Run `deploy/backup.sh` against `rgnr8-db`, e.g. as the same
   throwaway image job with `pg_dump` to an off-box location. Or rely on the fact that
   `rgnr8-db-v2` already holds the full copy plus a week of Render's own daily backups.
2. Remove the `rgnr8-db` block from `render.yaml` and merge. Render's docs don't say
   whether removing a database from a Blueprint deletes it. If it's still listed after
   the sync, **delete it in the dashboard** (Settings → Delete Database). That's when
   the $10.30/month stops.

---

## Alternative: Neon free tier (saves the full ≈$10/month)

Neon's free plan (0.5 GB storage, scales to zero) would hold this database at no cost.

**Trade-offs:**

- **Another vendor.** Billing, credentials and its own status page to watch.
- **Crosses the internet.** Traffic goes over TLS, out of Render's private network.
- **Cold starts.** About a second after idle.
- **Shorter recovery window** on the free tier than Render's daily backups. For a
  client's books, that matters.

**Steps:** create a Neon project in **AWS us-west-2** (closest to Render Oregon), run
the same copy job with `NEW_URL` set to the Neon connection string (append
`?sslmode=require`), then replace the three `fromDatabase` references with `sync: false`
env vars holding the Neon URL. Recommendation: stay on Render. The $6 difference buys
private networking and the backups.
