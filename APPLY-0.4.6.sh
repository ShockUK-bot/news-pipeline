#!/usr/bin/env bash
# Apply v0.4.6 (CIK->ticker mapping) on the Spark, git-native workflow.
# Run from the unzipped patch dir:  bash APPLY-0.4.6.sh
set -euo pipefail
PIPELINE=/opt/pipeline

echo "== 1. stop the EDGAR consumer path =="
sudo systemctl stop c1-ingestion

echo "== 2. copy files =="
for f in src/c1_ingestion/cik_map.py src/c1_ingestion/sources/edgar.py \
         config/sources.yaml tests/unit/test_cik_map.py \
         tests/conftest.py pyproject.toml; do
  sudo install -D -o trader -g trader "$f" "$PIPELINE/$f"; echo "   $f"
done
sudo install -d -o trader -g trader $PIPELINE/var

echo "== 3. tests against trading_test (guard-enforced) =="
DSN=$(sudo grep '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2-)
TDSN=$(echo "$DSN" | sed 's|/trading$|/trading_test|')
cd $PIPELINE
sudo -u trader bash -c "PIPELINE_DSN=$TDSN EMBEDDER=hash \
  QDRANT_PATH=/tmp/qdrant-test EDGAR_CONTACT=test@example.com \
  .venv/bin/python -m pytest tests/ -q" || {
    echo 'TESTS FAILED — c1 left stopped'; exit 1; }

echo "== 4. commit + push from the Spark (the canonical loop) =="
sudo -u trader git -C $PIPELINE add -A src config tests pyproject.toml
sudo -u trader git -C $PIPELINE commit -m "v0.4.6: CIK->ticker mapping (deterministic EDGAR symbols, skip_unmapped down-routing, test-DB guard)"
sudo -u trader git -C $PIPELINE push origin main

echo "== 5. restart =="
sudo systemctl start c1-ingestion
sleep 8
journalctl -u c1-ingestion -n 10 --no-pager | grep -E 'cik map|C1 up' || true

echo "== verify after ~15 min: =="
cat <<'VERIFY'
  psql "$DSN" -c "SELECT headline, symbols FROM news.news_items
    WHERE source='edgar' AND received_ts > now() - interval '15 min'
    ORDER BY received_ts DESC LIMIT 10;"
  # 8-Ks from listed companies should show tickers; trust/fund filings
  # should be absent from the triage queue entirely.
VERIFY
