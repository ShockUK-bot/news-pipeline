# News Pipeline — C1 Ingestion + C2 Dedup (Phase 1)

Implements Phase 1 of `trading-system-baseline` v0.5: the C1 ingestion service
(Alpaca news websocket via `*` wildcard firehose, SEC EDGAR poller, RSS) and the
C2 dedup/clustering service, writing to the validated `news` + `queue` Postgres
schemas and speaking the `news_item/1` / `signal.dedup/1` / `signal.triage/1`
contracts from `queue-contracts-spec.md`.

**Validated:** 97 tests — 45 unit + 52 integration across Phases 1-3 against a live PostgreSQL 16
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
cp env.example .env              # then edit
```

Database (either):
- `docker compose -f ops/docker-compose.yml up -d` — schemas auto-apply on first boot, or
- native PG16: `psql -f schema/news-store-schema.sql && psql -f schema/journal-schema.sql`

## Environment (see `env.example`)

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


## Phase 2 — A1 Triage + Router (observe-only)

Consumes `signal.triage` (DedupedSignal), runs grammar-constrained triage
against the Fast-slot model, journals a TRIAGE decision for every item
(ESCALATE / DISCARD / REJECT — nothing is silent), and routes TriagedSignals
per the four deterministic §6 rules onto `signal.guard` / `signal.thesis` /
`signal.analyst` / `signal.overnight`. No trading.

Run: `python -m a1_triage.service` (after C1/C2; config in `config/a1.yaml`).

**Model serving (Spark).** The Fast slot runs Qwen3-8B-Instruct Q6_K under
llama.cpp's server with the JSON-Schema grammar constraint:

```bash
# one-time: fetch the GGUF (or convert; ~6.9GB at Q6_K)
llama-server -m qwen3-8b-instruct-q6_k.gguf --host 127.0.0.1 --port 8080 \
  -c 8192 --parallel 2
```

Then in `.env` / `pipeline.env`: nothing — `config/a1.yaml` already points at
`http://127.0.0.1:8080`. Set `model.backend: stub` in a1.yaml for
model-server-less dev. Grammar enforcement is server-side during decoding;
the wrapper still validates code-side and retries once with the error appended
before journaling a REJECT (models propose, code disposes).

**Design notes:**
- Decision + routing fan-out commit in ONE transaction — a journaled decision
  without its routes (or vice versa) is impossible.
- `config_version` is real from Phase 2: the git SHA at service start is
  registered in `journal.config_versions` and stamped on every decision.
- Routing facts are code: NYSE calendar via pandas-market-calendars
  (holiday-correct), open-position intersection (empty until Phase 4),
  thesis matches (stub until Phase 8), `priority_score` with PLACEHOLDER
  weights in `config/a1.yaml` (real values are a Phase-4-gating item).
- Guard fan-out survives DISCARD: an immaterial item touching a held name
  still reaches `signal.guard` — corrections on held positions must reach A12.
- Material items are promoted to the `retrieval` collection at triage time
  (idempotent upsert; safe under at-least-once redelivery).


## Phase 3 — A2 Analyst + C3 Gate + C8 Regime (observe-only)

Run: `python -m a2_analyst.service`, `python -m c3_gate.service`,
`python -m c8_regime.service`. Configs: `config/a2.yaml`, `config/gate.yaml`,
`config/c8.yaml`. Downstream `signal.risk` accumulates until Phase 4's A3.

**Market data (locked decision):** Alpaca Market Data API, free IEX feed,
behind the `MarketData` protocol (`common/marketdata.py`; `MARKETDATA=fake`
for dev/tests). ACCEPTED CAVEAT: IEX is ~2-3% of consolidated volume — C3's
volume multiples run on a biased-but-consistent sample. MUST REVISIT (SIP or
Polygon) before real capital.

**Analyst slot (locked decision):** Qwen3-32B Q5_K_M, second llama-server:

```bash
llama-server -m qwen3-32b-q5_k_m.gguf --host 127.0.0.1 --port 8081 -c 16384
```

**A2** fetches the item's latest revision from the news store (TriagedSignal
carries only item_ref), builds a code-computed context pack (price action
since received_ts, related headlines from the retrieval collection, latest C8
regime features; sector/earnings/short-interest keys present but null until
their P1 sources land), and produces a strict-typed thesis. The mandatory
priced-in question is answered against provided numbers. **machine_checkable
invalidations are validated against the MIP DSL at authoring time** — stdlib
predicate names or full specs through `invalidation_dsl.validate()`; an
unmonitorable invalidation is a retry-then-REJECT, never a journal row.
`related_opportunities` emit SyntheticSignals (§10) on `signal.synthetic`;
A1's second consumer re-triages them for the sympathy ticker with
`derived_from` lineage — same gates, no shortcuts.

**C3** rules in order: LONG_ONLY (down-theses journal a veto — information,
not entries), CREDIBILITY (matrix: required outlets = f(impact bucket, tier);
Tier-1 alone passes; high source_risk bumps one level), then intraday
(move/volume/window/extended) or open-handoff (15-min blackout, gap-vs-
estimate PRICED_IN). Vetoes journal with full numbers and emit NO message
(§8). PASS journals + enqueues the GatePass with the pricing snapshot A3's
limit orders will use. `config/gate.yaml` values are PLACEHOLDERS pending the
§14 threshold design item; the 6% extended-skip is the baseline's own number.

**C8** writes `journal.regime_snapshots` on a market-hours-aware schedule
from ETF proxies (SPY 50d trend + slope, `realized_vol_20d` — honest naming,
NOT a fake `vix` — sector-ETF breadth fraction, top/bottom sector RS).
Every A2 decision references the latest `regime_id`.

## Known deferred items (by design, per baseline)

- IEX volume bias (SIP/Polygon upgrade) — before real capital.
- LULD halt feed — Phase 4 with C4;
  the `market.halt` queue contract is already defined.
- Exchange-calendar precision, dead-man thresholds, C7 alerting jobs — later
  phases per the build order.

## Phase 4: Execution layer (v0.4.0)

A3 risk sizing and C4 execution: the pipeline now trades (paper).

- `src/common/broker.py` — Alpaca paper / FakeBroker behind one protocol
- `src/a3_risk/` — hard gates -> bounded LLM discretion (band-validated,
  deterministic fallback) -> sizing chain (risk budget, 7 clips, viability)
- `src/c4_exec/` — entry flow (idempotent at intent_id AND client_order_id),
  two-tier stops re-materialized off the actual fill, exit engine
  (L1 stop/L2 ratchets/L3 time/L4 realization/L5 MIP invalidations),
  cancel-and-replace with 45s reinstatement, D1 overnight rule,
  reconciliation (broker is source of truth), drawdown breaker (-2%),
  dead-man ladder (ALERT -> BLOCK_ENTRIES -> never auto-flatten)
- `ops/RUNBOOK.md`, `ops/backup.sh` (restore test EXECUTED in validation),
  systemd units incl. nightly backup timer

Run tests: `pytest tests/` (177). Env: `BROKER=fake|alpaca` added.
Alpaca paper needs `ALPACA_KEY_ID`/`ALPACA_SECRET_KEY` in `.env`.

