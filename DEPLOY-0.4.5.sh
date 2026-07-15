#!/usr/bin/env bash
# Deploy 0.4.5 — EDGAR revision-storm fix (2026-07-14)
# Run from the unzipped patch directory on the Spark:  bash DEPLOY-0.4.5.sh
set -euo pipefail

PIPELINE=/opt/pipeline
DSN=$(grep '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2-)

echo "== 1. stop consumers (llama servers stay up) =="
sudo systemctl stop a1-triage c2-dedup c1-ingestion

echo "== 2. back up the files being replaced =="
STAMP=$(date +%Y%m%d-%H%M%S)
sudo tar czf /opt/pipeline-backup-pre-0.4.5-$STAMP.tgz -C $PIPELINE \
  src/c1_ingestion/store.py src/c1_ingestion/normalize.py \
  src/c1_ingestion/sources/edgar.py src/c2_dedup/cluster.py \
  src/c2_dedup/service.py config/sources.yaml \
  tests/integration/test_lifecycle.py pyproject.toml
echo "   backup: /opt/pipeline-backup-pre-0.4.5-$STAMP.tgz"

echo "== 3. copy patched files =="
for f in src/c1_ingestion/store.py src/c1_ingestion/normalize.py \
         src/c1_ingestion/sources/edgar.py src/c2_dedup/cluster.py \
         src/c2_dedup/service.py config/sources.yaml \
         tests/integration/test_lifecycle.py \
         tests/integration/test_edgar_storm_regression.py pyproject.toml; do
  sudo install -D -o trader -g trader "$f" "$PIPELINE/$f"
  echo "   $f"
done

echo "== 4. run the test suite on the Spark (hash embedder, real PG) =="
cd $PIPELINE
sudo -u trader bash -c "set -a; source /etc/pipeline/pipeline.env; \
  EMBEDDER=hash EDGAR_CONTACT=\$EDGAR_CONTACT \
  .venv/bin/python -m pytest tests/ -q" || {
    echo 'TESTS FAILED — services left stopped; restore from the backup tarball'; exit 1; }

echo "== 5. purge the stale triage backlog (storm remnants) =="
psql "$DSN" -c "UPDATE queue.messages SET done_ts=now(),
  last_error='expired: 0.4.5 deploy purge'
  WHERE queue_name IN ('signal.triage','signal.dedup')
  AND done_ts IS NULL AND claimed_ts IS NULL;"

echo "== 6. restart services =="
sudo systemctl start c1-ingestion c2-dedup a1-triage
sleep 5
systemctl is-active c1-ingestion c2-dedup a1-triage

echo "== done. Verification (run after ~30 min): =="
cat <<'VERIFY'
  # inflow should now be a few hundred/day, not thousands/hour:
  psql "$DSN" -c "SELECT date_trunc('hour', enqueued_ts), count(*)
    FROM queue.messages WHERE queue_name='signal.triage'
    AND enqueued_ts > now() - interval '3 hours' GROUP BY 1 ORDER BY 1;"

  # no EDGAR revisions ever again:
  psql "$DSN" -c "SELECT count(*) FROM news.news_items
    WHERE source='edgar' AND revision > 1
    AND received_ts > now() - interval '1 hour';"   -- must be 0

  # archived-not-enqueued Form 4s visible in the C1 log:
  journalctl -u c1-ingestion --since '-30 min' | grep archived_only | tail -5

  # duplicates being dropped by C2:
  journalctl -u c2-dedup --since '-30 min' | grep 'duplicate dropped' | tail -5

  # and the original symptom: GPU should now idle between news bursts
  nvidia-smi dmon -s um -d 5
VERIFY
