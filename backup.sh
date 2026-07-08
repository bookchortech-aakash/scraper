#!/usr/bin/env bash
#
# backup.sh — rotating Postgres backup for the config-driven scraper.
#
# Runs pg_dump INSIDE the postgres container (custom compressed format), so it
# needs no client tools on the host and always matches the server version. The
# dump is written to ./backups on the host, then old backups are rotated out —
# but ONLY after the new dump passes two health checks, so a run against an
# empty/broken DB can never erase your good history.
#
# Health gates before rotation:
#   1. size floor   — the dump must be at least MIN_BYTES (guards empty dumps)
#   2. cliff guard  — if the live `records` count is < half the previous
#                     backup's count, KEEP EVERYTHING and warn (a wipe can't
#                     silently roll off your good backups).
#
# Install (run from ~/scraper):
#   chmod +x backup.sh
#   ./backup.sh                      # take a backup now
#
# Cron (twice a day, 02:00 and 14:00) — edit with `crontab -e`:
#   0 2,14 * * * cd /home/aakash/scraper && ./backup.sh >> backups/cron.log 2>&1
#
# Tunables (env or edit below):
#   KEEP=4          how many backups to retain (4 = 2 days @ 2/day)
#   MIN_BYTES=1000  reject a dump smaller than this as "bad"
#   BACKUP_DIR=./backups
#
set -euo pipefail
cd "$(dirname "$0")"

DB_USER="${POSTGRES_USER:-scraper}"
DB_NAME="${POSTGRES_DB:-scraper}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP="${KEEP:-4}"
MIN_BYTES="${MIN_BYTES:-1000}"

mkdir -p "$BACKUP_DIR"
LOG="$BACKUP_DIR/backup.log"
ts="$(date +%Y%m%d_%H%M%S)"
out="$BACKUP_DIR/scraper_${ts}.dump"

log() { echo "$(date '+%F %T') | $*" | tee -a "$LOG" >&2; }

# --- resolve docker compose (v2 plugin) vs docker-compose (v1) -------------
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  log "ERROR: neither 'docker compose' nor 'docker-compose' found on PATH."
  exit 1
fi

is_int() { [[ "${1:-}" =~ ^[0-9]+$ ]]; }

# --- live record count (a sanity signal, also feeds the cliff guard) -------
rows="$($DC exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -tAc \
        'SELECT count(*) FROM records;' 2>/dev/null | tr -d '[:space:]' || true)"
is_int "$rows" || rows=""      # blank if the query failed (DB down, etc.)

log "starting backup -> $(basename "$out") (records=${rows:-unknown})"

# --- take the dump ---------------------------------------------------------
# -Fc = custom, compressed, restorable with pg_restore --clean.
if ! $DC exec -T postgres pg_dump -Fc -U "$DB_USER" "$DB_NAME" > "$out" 2>>"$LOG"; then
  log "ERROR: pg_dump failed. Removing partial file; OLD BACKUPS KEPT."
  rm -f "$out"
  exit 1
fi

size="$(stat -c%s "$out" 2>/dev/null || stat -f%z "$out" 2>/dev/null || echo 0)"

# --- gate 1: size floor ----------------------------------------------------
if [ "$size" -lt "$MIN_BYTES" ]; then
  log "ERROR: dump is only ${size}B (< ${MIN_BYTES}B). Marking .bad; OLD BACKUPS KEPT."
  mv "$out" "$out.bad"
  exit 1
fi

# stamp the row count next to the dump (for the cliff guard + your own audit)
[ -n "$rows" ] && echo "$rows" > "$out.rows"
log "dump ok: ${size} bytes, ${rows:-unknown} records"

# --- gate 2: cliff guard ---------------------------------------------------
# Compare against the most recent PREVIOUS good dump's recorded row count.
prev_rows=""
prev_sidecar="$(ls -1t "$BACKUP_DIR"/scraper_*.dump.rows 2>/dev/null \
                | grep -v "scraper_${ts}.dump.rows" | head -1 || true)"
[ -n "$prev_sidecar" ] && prev_rows="$(tr -d '[:space:]' < "$prev_sidecar" || true)"

do_rotate=1
if is_int "$rows" && is_int "$prev_rows" && [ "$prev_rows" -gt 0 ]; then
  if [ "$rows" -lt $(( prev_rows / 2 )) ]; then
    do_rotate=0
    log "WARNING: records fell ${prev_rows} -> ${rows} (>50% drop). NOT rotating."
    log "         Old backups are preserved. Investigate before trusting this dump."
  fi
fi

# --- rotate: keep newest $KEEP dumps, delete older (with sidecars) ---------
if [ "$do_rotate" -eq 1 ]; then
  ls -1t "$BACKUP_DIR"/scraper_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) \
  | while IFS= read -r old; do
      log "rotate out: $(basename "$old")"
      rm -f "$old" "$old.rows"
    done
else
  log "rotation skipped (cliff guard); current backup count: $(ls -1 "$BACKUP_DIR"/scraper_*.dump 2>/dev/null | wc -l | tr -d ' ')"
fi

log "done. backups in $BACKUP_DIR:"
ls -1t "$BACKUP_DIR"/scraper_*.dump 2>/dev/null | sed 's/^/          /' | tee -a "$LOG" >&2 || true