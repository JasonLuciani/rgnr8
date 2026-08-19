#!/usr/bin/env bash
#
# Back up the RGNR8 Postgres database to a compressed, timestamped dump.
#
# A client's complete books live in this database, so a tested backup is not
# optional. This uses pg_dump's custom format (-Fc), which restores selectively
# and in parallel, and is safe to run against a live database (it takes a
# consistent snapshot in one transaction).
#
# Usage:
#   DATABASE_URL=postgres://user:pass@host:5432/db ./backup.sh [OUTPUT_DIR]
#
# Cron example (daily at 02:15 UTC), keeping 30 days:
#   15 2 * * *  DATABASE_URL=... /opt/rgnr8/deploy/backup.sh /var/backups/rgnr8
#
# Restore the newest dump with ./restore.sh (see that script).
set -euo pipefail

: "${DATABASE_URL:?set DATABASE_URL to the Postgres connection string}"
OUT_DIR="${1:-./backups}"
RETAIN_DAYS="${RGNR8_BACKUP_RETAIN_DAYS:-30}"

mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$OUT_DIR/rgnr8-$STAMP.dump"

echo "[backup] dumping to $FILE"
# -Fc custom format, --no-owner/--no-privileges so it restores cleanly into a
# database owned by a different role than production.
pg_dump "$DATABASE_URL" -Fc --no-owner --no-privileges -f "$FILE"

# Integrity check: pg_restore --list must be able to read the archive's table of
# contents, which fails loudly on a truncated or corrupt dump.
echo "[backup] verifying archive is readable"
pg_restore --list "$FILE" >/dev/null
echo "[backup] ok: $(du -h "$FILE" | cut -f1) $FILE"

# Prune old dumps.
if [ "$RETAIN_DAYS" -gt 0 ]; then
  echo "[backup] pruning dumps older than $RETAIN_DAYS days"
  find "$OUT_DIR" -name 'rgnr8-*.dump' -type f -mtime "+$RETAIN_DAYS" -print -delete || true
fi
echo "[backup] done"
