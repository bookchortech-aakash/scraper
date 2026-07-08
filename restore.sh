#!/usr/bin/env bash
#
# restore.sh — restore a scraper backup produced by backup.sh.
#
# Streams a custom-format dump into the postgres container and rebuilds the
# schema + data (existing objects are dropped and recreated).
#
# Usage (from ~/scraper):
#   ./restore.sh backups/scraper_20260708_140000.dump
#   ./restore.sh                       # picks the newest backup automatically
#
set -euo pipefail
cd "$(dirname "$0")"

DB_USER="${POSTGRES_USER:-scraper}"
DB_NAME="${POSTGRES_DB:-scraper}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"

if docker compose version >/dev/null 2>&1; then DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then DC="docker-compose"
else echo "ERROR: docker compose not found." >&2; exit 1; fi

f="${1:-}"
if [ -z "$f" ]; then
  f="$(ls -1t "$BACKUP_DIR"/scraper_*.dump 2>/dev/null | head -1 || true)"
  [ -n "$f" ] || { echo "No backups found in $BACKUP_DIR." >&2; exit 1; }
fi
[ -f "$f" ] || { echo "Not a file: $f" >&2; exit 1; }

rows="$( [ -f "$f.rows" ] && cat "$f.rows" || echo unknown )"
echo "About to restore: $f"
echo "  target DB : $DB_NAME"
echo "  records   : $rows (as recorded when the backup was taken)"
echo "  NOTE: existing tables in $DB_NAME will be dropped and recreated."
read -r -p "Type 'restore' to proceed: " ok
[ "$ok" = "restore" ] || { echo "aborted."; exit 1; }

# --clean --if-exists so a partial/empty current DB doesn't block the restore.
$DC exec -T postgres pg_restore --clean --if-exists --no-owner \
    -U "$DB_USER" -d "$DB_NAME" < "$f"

echo "restore complete."
$DC exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -tAc \
    'SELECT count(*) FROM records;' | xargs echo "records now in DB:"