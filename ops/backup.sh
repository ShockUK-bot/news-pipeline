#!/usr/bin/env bash
# Nightly pg_dump, custom format, 14-day rotation (phase4-design D5).
# Cron/systemd-timer: 02:30 local. Journals its own success into health.
set -euo pipefail
DSN="${PIPELINE_DSN:-postgresql://trader:trader_dev@127.0.0.1:5432/trading}"
DIR="${BACKUP_DIR:-$HOME/pipeline-backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
mkdir -p "$DIR"
STAMP=$(date +%Y%m%d)
OUT="$DIR/trading-$STAMP.dump"
pg_dump -Fc -d "$DSN" -f "$OUT"
SIZE=$(stat -c%s "$OUT")
find "$DIR" -name 'trading-*.dump' -mtime +"$KEEP_DAYS" -delete
psql "$DSN" -qc "INSERT INTO journal.health (component, status, detail, updated_ts)
  VALUES ('backup','OK','$OUT ($SIZE bytes)', now())
  ON CONFLICT (component) DO UPDATE
  SET status='OK', detail=EXCLUDED.detail, updated_ts=now();"
echo "backup OK: $OUT ($SIZE bytes)"

