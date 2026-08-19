#!/usr/bin/env bash
#
# Restore a RGNR8 Postgres dump produced by backup.sh — and, by making restore a
# one-command operation, make it something you actually test. An untested backup
# is a guess; run this against a scratch database on a schedule and confirm the
# app boots against the result.
#
# Usage:
#   TARGET_DATABASE_URL=postgres://user:pass@host:5432/scratch ./restore.sh DUMP_FILE
#   # or restore the newest dump in a directory:
#   TARGET_DATABASE_URL=... ./restore.sh --latest ./backups
#
# WARNING: --clean drops and recreates every object in the target first. Point
# TARGET_DATABASE_URL at a restore/scratch database, never production, unless you
# are running a real recovery.
set -euo pipefail

: "${TARGET_DATABASE_URL:?set TARGET_DATABASE_URL to the database to restore INTO}"

if [ "${1:-}" = "--latest" ]; then
  DIR="${2:-./backups}"
  DUMP="$(ls -1t "$DIR"/rgnr8-*.dump 2>/dev/null | head -n1 || true)"
  [ -n "$DUMP" ] || { echo "[restore] no dumps found in $DIR" >&2; exit 1; }
else
  DUMP="${1:?pass a dump file, or --latest DIR}"
fi

[ -f "$DUMP" ] || { echo "[restore] no such dump: $DUMP" >&2; exit 1; }

echo "[restore] restoring $DUMP into $TARGET_DATABASE_URL"
# --clean --if-exists so a re-restore is idempotent; --no-owner/--no-privileges
# so it lands cleanly regardless of the production role names.
pg_restore --clean --if-exists --no-owner --no-privileges \
  --dbname "$TARGET_DATABASE_URL" "$DUMP"

echo "[restore] done — now smoke-test: point the app's RGNR8_DATABASE_URL at the"
echo "          restored database and hit /ready, then sign in and open the books."
