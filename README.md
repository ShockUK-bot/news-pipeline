# News Pipeline — C1 Ingestion + C2 Dedup (Phase 1)

Implements Phase 1 of `trading-system-baseline` v0.5: the C1 ingestion service
(Alpaca news websocket via `*` wildcard firehose, SEC EDGAR poller, RSS) and the
C2 dedup/clustering service, writing to the validated `news` + `queue` Postgres
schemas and speaking the `news_item/1` / `signal.dedup/1` / `signal.triage/1`
contracts from `queue-contracts-spec.md`.

**Validated:** 23 unit tests + 11 integration tests against a live PostgreSQL 16
instance (the integration suite replays the full news-lifecycle story through the
actual service code: store → transactional enqueue → dedup → cluster →
corroboration → DLQ → prune → gap tracking).

---

## Layout

```
config/           sources.yaml (feeds, tiers, gap thresholds), dedup.yaml
schema/           vendored copies of the validated news-store + journal DDL
src/common/       clock (UTC discipline), contracts (Pydantic mirrors of the
                  JSON contracts), db (pool), queue (claim/ack/fail/enqueue), log
src/c1_ingestion/ service supervisor, normalize, store, heartbeat, sources/
src/c2_dedup/     service consumer, embedder, vectorstore (Qdrant), cluster
tests/            unit (no DB) + integration (real PG16)
ops/              systemd units, docker-compose (PG16 + Qdrant)
```

## Setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"           # runtime + pytest
pip install -e ".[embed]"         # sentence-transformers (Spark; downloads bge model)
cp .env.example .env              # then edit
```

Database (either):
- `docker compose -f ops/docker-compose.yml up -d` — schemas auto-apply on first boot, or
- native PG16: `psql -f schema/news-store-schema.sql && psql -f schema/journal-schema.sql`

## Environment (see `.env.example`)

| Var | Purpose |
|---|---|
| `PIPELINE_DSN` | Postgres connection string |
| `ALPACA_KEY_ID` / `ALPACA_SECRET_KEY` | Alpaca paper-account keys (news stream auth) |
| `EDGAR_CONTACT` / `EDGAR_APP_NAME` | SEC fair-access User-Agent (`Ians Trading System ian.gillbanks@gmail.com`) — C1 fails fast if unset |
| `QDRANT_URL` | empty = local mode at `QDRANT_PATH`; set to `http://127.0.0.1:6333` for the server |
| `EMBEDDER` | `bge` (production) or `hash` (deterministic, dev/test) |

## Run

```bash
export PYTHONPATH=src && set -a && source .env && set +a
python -m c1_ingestion.service     # terminal 1
python -m c2_dedup.service         # terminal 2
```

Watch it work:
```sql
SELECT item_id, revision, source, source_tier, headline
  FROM news.news_items ORDER BY received_ts DESC LIMIT 10;
SELECT queue_name, count(*) FILTER (WHERE done_ts IS NULL) AS pending
  FROM queue.messages GROUP BY 1;
SELECT * FROM news.cluster_corroboration ORDER BY cluster_id DESC LIMIT 5;
SELECT * FROM journal.health;
SELECT * FROM news.quarantine WHERE NOT reviewed;
SELECT * FROM news.ingestion_gaps ORDER BY gap_id DESC LIMIT 5;
```

## Test

```bash
export PYTHONPATH=src PIPELINE_DSN=postgresql://trader:trader_dev@127.0.0.1:5432/trading
export EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test
pytest tests/unit -q               # 23 tests, no DB needed
pytest tests/integration -q        # 11 tests, TRUNCATES news/queue tables — dev DB only
```

**Real-embedder smoke test (run once on your machine / the Spark):**
```bash
pip install -e ".[embed]"
EMBEDDER=bge python - <<'EOF'
from c2_dedup.embedder import get_embedder
e = get_embedder()
a = e.embed("Acme Corp announces $2B buyback")
b = e.embed("Acme announces two billion dollar share repurchase")
import math; dot = sum(x*y for x,y in zip(a,b))
print(f"bge OK, dim={len(a)}, paraphrase similarity={dot:.3f}")   # expect > 0.8
EOF
```

## Spark deployment checklist

1. `git clone` to `/opt/pipeline`; create venv; `pip install -e ".[embed]"`.
2. Native PG16 on NVMe (baseline §11.3); apply both schema files; create the
   `trader` role with a real password.
3. Qdrant server via docker; set `QDRANT_URL=http://127.0.0.1:6333`.
4. Secrets into `/etc/pipeline/pipeline.env` (mode 600, owner trader) — never in
   the config repo (§11.6).
5. `EMBEDDER=bge`; run the smoke test above once to download the model.
6. Copy `ops/systemd/*.service` to `/etc/systemd/system/`; `systemctl enable --now
   c1-ingestion c2-dedup`.
7. Verify: the health SQL above; then `journalctl -u c1-ingestion -f` during
   market hours — Alpaca frames should flow within seconds of connect.

## Design notes (decisions embedded in this code)

- **Transactional store+enqueue.** `news_items` insert and the `signal.dedup`
  enqueue commit atomically; a crash between them is impossible. At-least-once
  everywhere downstream, with `{item_id}:{revision}` dedup keys end-to-end.
- **Revisions**: same `item_id` + changed `content_hash` → revision N+1,
  `supersedes` set, `is_correction=true`; unchanged hash → no-op echo (feed
  replays after reconnect cost nothing). Content hash is NFKC + casefold +
  whitespace-collapsed, so cosmetic reformatting isn't a fake revision.
- **Quarantine, never drop** (v0.4): every normalization failure lands in
  `news.quarantine` with a reason code; queue poison messages DLQ there too
  (source `queue:<name>`), which is what C7 will alert on.
- **Gap semantics differ by source type.** The websocket tracks message
  silence (market-hours-aware thresholds); pollers track fetch success — for a
  poller, "publisher quiet" is normal, "cannot fetch" is the gap.
- **EDGAR fair access**: mandatory User-Agent from env, 15s polling, 429/503
  respected. Amended filings are new accession numbers → new items (an
  amendment is a new filing event, not a text revision).
- **Two collections** (v0.4): `dedup_48h` pruned hourly to the trailing window;
  `retrieval` implemented (`promote_to_retrieval()`, `related()`) but admission
  is A1's material flag, so its writer arrives in Phase 2.
- **Cluster thresholds**: ≥0.90 duplicate, 0.80–0.90 corroborating coverage of
  the same story, <0.80 new story. Both in `config/dedup.yaml`, tunable through
  the normal config channel. `independent_outlets` comes from the
  `cluster_corroboration` view so the DedupedSignal always carries current
  numbers for C3's credibility rule.
- **UTC everywhere** (§11.5): all timestamp creation/parsing flows through
  `common/clock.py`; naive timestamps are rejected into quarantine. Market-hours
  is deliberately coarse in Phase 1 (gap thresholds only); the exchange-calendar
  library lands with C3/C4 where holiday precision is load-bearing.

## Known deferred items (by design, per baseline)

- Router (TriagedSignal routing rules, position-touching guard path) — Phase 2
  with A1; C1 enqueues correction revisions normally until then.
- `retrieval` collection writer — Phase 2 (A1 material flag is the admission).
- LULD halt feed — arrives with the market-data provider integration (C3/C4);
  the `market.halt` queue contract is already defined.
- Exchange-calendar precision, dead-man thresholds, C7 alerting jobs — later
  phases per the build order.
