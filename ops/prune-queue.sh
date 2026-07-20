#!/usr/bin/env bash
# Prune COMPLETED queue.messages rows older than N days.
#
# queue.messages is transport, not the audit log: ack/DLQ sets done_ts but
# never deletes the row, so the table grows without bound. The permanent
# record of every decision lives in journal.decisions — pruning done queue
# rows loses nothing operationally. Only rows with done_ts set are ever
# touched; pending / in-flight work is never removed.
#
# systemd-timer: 03:10 ET daily. Journals its result into journal.health so
# the C6 dashboard shows the last prune under component 'queue_prune'.
set -euo pipefail
DSN="${PIPELINE_DSN:-postgresql://trader:trader_dev@127.0.0.1:5432/trading}"
KEEP_DAYS="${QUEUE_PRUNE_KEEP_DAYS:-7}"

# guard: KEEP_DAYS must be a plain integer (it is interpolated into SQL).
if ! [[ "$KEEP_DAYS" =~ ^[0-9]+$ ]]; then
  echo "queue prune FAILED: QUEUE_PRUNE_KEEP_DAYS='$KEEP_DAYS' is not an integer" >&2
  exit 1
fi

DELETED=$(psql "$DSN" -qtAc "
  WITH del AS (
    DELETE FROM queue.messages
    WHERE done_ts IS NOT NULL
      AND done_ts < now() - (interval '1 day' * ${KEEP_DAYS})
    RETURNING 1)
  SELECT count(*) FROM del;")

psql "$DSN" -qc "INSERT INTO journal.health (component, status, detail, updated_ts)
  VALUES ('queue_prune','OK','pruned ${DELETED} done rows older than ${KEEP_DAYS}d', now())
  ON CONFLICT (component) DO UPDATE
  SET status='OK', detail=EXCLUDED.detail, updated_ts=now();"

echo "queue prune OK: deleted ${DELETED} done rows older than ${KEEP_DAYS}d"
