#!/usr/bin/env bash
# Offsite copy of the newest nightly dump to Google Drive via rclone (v0.25.2).
# Remote "gdrive" lives in ~/.config/rclone/rclone.conf (operator authorised).
# Keeps OFFSITE_KEEP_DAYS on the remote. Never raises: an unconfigured remote
# writes a DEGRADED health row (shows in the evening digest) and exits 0.
set -uo pipefail
DSN="${PIPELINE_DSN:?PIPELINE_DSN required}"
DIR="${BACKUP_DIR:-$HOME/pipeline-backups}"
REMOTE="${OFFSITE_REMOTE:-gdrive:spark-pipeline-backups}"
KEEP="${OFFSITE_KEEP_DAYS:-21}"
RCLONE="${RCLONE_BIN:-$HOME/.local/bin/rclone}"
health() {  # status detail
  psql "$DSN" -qc "INSERT INTO journal.health (component, status, detail, updated_ts)
    VALUES ('backup_offsite', '$1', \$\$$2\$\$, now())
    ON CONFLICT (component) DO UPDATE SET status=EXCLUDED.status, detail=EXCLUDED.detail, updated_ts=now();"
}
if [ ! -x "$RCLONE" ]; then health DEGRADED "rclone not installed at $RCLONE"; exit 0; fi
if ! "$RCLONE" listremotes 2>/dev/null | grep -q "^${REMOTE%%:*}:"; then
  health DEGRADED "remote ${REMOTE%%:*} not configured: run the Google Drive authorisation (docs/claude_backup-offsite-2026-09-22.md)"; exit 0
fi
LATEST=$(ls -1t "$DIR"/trading-*.dump 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then health DEGRADED "no local dump in $DIR"; exit 0; fi
if "$RCLONE" copy "$LATEST" "$REMOTE/" --transfers 1 --retries 3 --low-level-retries 5 --stats-one-line -q; then
  "$RCLONE" delete "$REMOTE/" --min-age "${KEEP}d" -q || true
  COUNT=$("$RCLONE" lsf "$REMOTE/" 2>/dev/null | wc -l)
  SIZE=$(stat -c%s "$LATEST")
  health OK "$(basename "$LATEST") ($SIZE bytes) uploaded; $COUNT dumps on $REMOTE"
  echo "offsite OK: $(basename "$LATEST") -> $REMOTE ($COUNT kept)"
else
  health DEGRADED "upload of $(basename "$LATEST") to $REMOTE failed"; echo "offsite FAILED"; exit 0
fi
