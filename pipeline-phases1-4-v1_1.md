# News Pipeline — Phases 1–4 (v1.1)

**Consolidated implementation document.** Replaces `pipeline-phases1-4-v1_0.md`
in project knowledge. Repo: `ShockUK-bot/news-pipeline` (private). Version 0.4.5.

## v1.1 changelog — the 2026-07-14 EDGAR revision-storm fix

Root cause found by live diagnosis on the Spark (GPU pinned at 96% around the
clock): EDGAR's current-events index lists one filing once PER ASSOCIATED
ENTITY; alternating Filer/Subject rows under the same accession defeated
store_item's latest-hash revision check, minting a revision every 15s poll
(rev 58+ on one 13G; ~80k items/day; A1 saturated at its ~2k/hr ceiling;
84,856-deep triage backlog; C2 detected the duplicates and forwarded anyway).

Fixes (all covered by tests/integration/test_edgar_storm_regression.py, 11
tests replaying the incident data; full suite 189 green):
1. **EDGAR immutability + entity merge** — poller groups index rows by
   accession into ONE item (entities preserved in raw["entities"], canonical
   headline prefers the company row); `store_item(immutable=True)` makes any
   re-seen accession an unconditional no-op. No revisions from the EDGAR
   path, ever. Alpaca/RSS revision semantics unchanged.
2. **C2 enforces the drop** — duplicate verdicts (sim >= 0.90) no longer
   forward to triage; cluster membership + corroboration still recorded for
   C3's credibility view. Corroboration band (0.80–0.90) still forwards.
3. **Form whitelist** (config: edgar.triage_forms, prefix match) — only
   event-class filings enter the pipeline; Form 4 / 424B2 / 144 / 13F etc.
   are archived to news_items with enqueue=False.
4. **Multi-word form parsing** — "SCHEDULE 13G/A" titles previously failed
   the form regex and lost their form channel; parsing now also extracts
   entity name / CIK / role.
Deferred to next session: CIK→ticker mapping (deterministic symbol stamping
from SEC company_tickers.json); clustering granularity for formulaic filing
headlines.

## Scope

- **Phase 1** — C1 Ingestion (Alpaca `*` firehose, SEC EDGAR, RSS Tier-3;
  revisions, quarantine, gap monitors) + C2 Dedup (Qdrant, cluster
  corroboration).
- **Phase 2** — A1 Triage (Fast slot, grammar-constrained) + in-process router
  (four §6 rules, one-transaction decision+fan-out).
- **Phase 3** — A2 Analyst + C3 Market Confirmation Gate + C8 Regime Context.
- **Phase 4 (NEW)** — A3 Risk/PM sizing + C4 Execution: the pipeline trades
  (Alpaca paper). Entry flow, two-tier stops, exit engine, overnight rule,
  reconciliation, dead-man ladder, drawdown breaker, runbook + verified backup.

## Phase 4 locked decisions (phase4-design-v1_0, D1–D7)

1. **D1 overnight rule** `eod_rule_v1` at 15:45 ET, SHORT lane only:
   earnings next session → EXIT; unrealized ≥ +0.3R → HOLD; age < 1 session
   AND realized fraction of predicted move < 0.5 → HOLD; else EXIT
   (limit-at-bid, reprice pass 15:55, unfilled → OVERNIGHT_FORCED_HOLD with
   catastrophe intact). LONG lane: default hold, no decision rows.
2. **D2 capital & limits** (config/risk.yaml, git-only): risk 0.5%/trade of
   effective capital = min(broker equity, trading_capital); notional cap 15%;
   heat 3% total split SHORT 2% / LONG 1%; sector 1.5% (dormant —
   SECTOR_UNKNOWN until a sector source lands); viability ≥ 50% of intended
   risk else SIZE_CLIPPED. Operational controls in `journal.control`
   (dashboard-writable): trading_capital, max_trades_per_day (5),
   kill_switch, drawdown_breaker, block_entries. ADV ≤ 1%, spread ≤ 40bps,
   15-min entry blackout before close.
3. **D3 catastrophe stops**: broker-resident stop-market GTC at
   entry_k + 1.5 (SHORT 3.5×ATR, LONG 4.5×ATR), placed on fill, NEVER moved.
   Exit sequence: cancel-cat → marketable-limit exit → unfilled in 45s →
   cancel exit → reinstate cat (journaled EXIT_REINSTATED). Race handled:
   cancel fails because cat already filled → record CATASTROPHE exit, done.
4. **D4 dead-man ladder**: ALERT → BLOCK_ENTRIES → NEVER auto-flatten.
   ingestion 3/10min; marketdata 2/2min + exit-engine suspend at 10min
   (catastrophe stops become sole protection — journaled loudly); models/gate
   5min alert-only. RTH escalation only; ownership rule — the monitor clears
   only blocks it set (`deadman_block` marker), operator blocks persist.
5. **D5 backup/runbook**: nightly pg_dump -Fc, 14-day rotation, health row;
   `ops/RUNBOOK.md` covers Spark death, broker outage, restore, config
   rollback, breaker discipline, cold-start order. **Restore test EXECUTED
   in validation** (dump → drop → restore → verify, counts matched).
6. **D6 exit profiles** (config/exit_profiles.yaml): short_term_v1
   (stop 2.0×ATR, cat 3.5, BE at +1R, trail from +1.5R at 2.5×ATR, time stop
   thesis-window/+0.5R, realization 0.7×magnitude → scale_out_50,
   eod_rule_v1) and long_term_v1 (3.0/4.5, trail from +2R at 4.0×ATR-weekly,
   no time stop, review_flag, default_hold). A3 discretion bands:
   k ∈ [1.5, 2.5], realization_fraction ∈ [0.5, 0.9], time_window ∈ [1, 3]
   sessions — model output outside bands falls back to profile defaults,
   journaled; the trade never blocks on the model.
7. **D7 deferrals confirmed**: earnings calendar nullable (None → allow +
   EARNINGS_UNKNOWN flag); LULD halt heuristic-only (no bars >10min RTH →
   HALT_FROZEN, evaluations freeze, resume on bar); breaker −2% daily
   one-way; Alpaca paper (`paper-api` hard-coded until real-capital
   decision).

## Phase 4 mechanics worth remembering

- **intent_id = sha256(signal_id:revision:config_version)[:24]** — idempotent
  at BOTH layers: journal `intents` PK short-circuits A3 replays; broker
  `client_order_id` short-circuits C4 replays. A config change deliberately
  mints a new intent.
- **A3 order of operations**: parse → controls/heat reads → build sizing
  inputs → **hard gates BEFORE the model call** (no tokens under a kill
  switch; operational vetoes dominate journal semantics) →
  NO_THESIS_LINEAGE veto (positions.thesis_decision_id is NOT NULL by
  design) → discretion → chain → one tx (decision + intent + enqueue).
- **A3 never calls the broker**: capital numbers come from C4's
  reconciliation rows in `journal.control` (broker_equity, settled_cash,
  last_reconcile_ts). Effective capital is DERIVED (min) at read time,
  never stored.
- **C4 stage is `ORDER`** in `journal.decisions` — the Phase-1 schema
  reserved the name; the design doc's "EXEC" is an erratum.
- **Stops re-materialize off the ACTUAL fill** (A3 priced off the snapshot
  ask; C4 re-computes initial + catastrophe from filled_avg_price).
- **Exit engine layering** (strict priority per bar): L1 synthetic stop
  (attribution via `stop_basis`: initial→STOP, breakeven→BREAKEVEN,
  trail→TRAIL; stop beats target and invalidation on the same bar) → L5 MIP
  invalidations → L3 time stop → L4 realization (scale_out_50 re-places the
  catastrophe for the remainder) → L2 tighten-only ratchets (a looser stop
  proposal is discarded, including from MIP tighten_stop fires).
- **Session-tf MIP predicates** (e.g. `close_below_prenews`) evaluate on the
  engine's session-close pass (after 16:00 ET, real daily bar), not on
  minute bars — intraday dips below prenews correctly do NOT fire them.
- **Runtime exit-policy state rides in `positions.exit_policy`**:
  current_stop, stop_basis, hwm, scale_out_done — every mutation journaled
  in position_events with r_progress.
- **FakeBroker order ids are globally unique** (`orders.broker_order_id` is
  UNIQUE; real brokers guarantee this, the fake must too).
- Test-suite queue hygiene (hard-won, twice): any test that drives A3 to a
  SIZE must drain its own `exec.intent` message — FIFO claims otherwise
  poison later tests.

## Sizing-chain observation (test-pinned)

At ATR ≈ 1.5% of price with k=2, risk-derived size is ~16.7% notional —
the 15% notional cap trims the clean path to ~89% of intended risk (still
viable). Lower-volatility names will routinely bind on `notional`; this is
the design working, not a bug. `binding_clip` is journaled per decision.

## Validation record

**177/177 tests** (58 unit + 119 integration/e2e) on live PostgreSQL 16.14,
fresh-database rebuild validated (drop → schemas → full suite). Restore test
executed live: decisions 9→9, news 1→1, PASSED. FakeBroker covers fills,
partials, rejects, resting orders, catastrophe races, drift injection;
AlpacaBroker awaits its Spark smoke test (`ops/alpaca-smoke.py`).

## Spark deployment (first session)

1. Clone; `.env` with PIPELINE_DSN, ALPACA_KEY_ID/SECRET_KEY (paper),
   EDGAR_CONTACT; `pip install -e .`
2. Postgres 16: apply `schema/news-store-schema.sql` then
   `schema/journal-schema.sql`; Qdrant up; llama-servers :8080/:8081.
3. `pytest tests/` — expect 177.
4. `PYTHONPATH=src python3 ops/alpaca-smoke.py` — auth + far-limit
   submit/cancel round-trip.
5. Set trading_capital + max_trades_per_day from the dashboard (or SQL on
   `journal.control`).
6. Cold-start order per RUNBOOK §6 (c4-exec LAST — it reconciles before
   consuming). Enable `pipeline-backup.timer`.
7. Begin the paper soak: multi-day unattended run is the real Phase 4 gate.

## Still open (deferred list)

Compounding promotion criteria; A12 auto-execution (Phase 5); gate threshold
tuning (§14); earnings-calendar source (removes EARNINGS_UNKNOWN); sector
source (activates sector-heat clip); priority_score weights (Phase 7); C9
replay spec; IEX→SIP before real capital.

---

# Source listing (v0.4.0, 127 files; generated from the repo)


## `README.md`

```markdown
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

```

## `config/a1.yaml`

```yaml
# A1 Triage + Router configuration (Phase 2, observe-only).
#
# NOTE ON VALUES: priority weights and the corroboration bonus are PLACEHOLDER
# starting points. Config starting values are a deferred design item gating
# Phase 4 (baseline §14); the formula SHAPE is fixed here, the numbers are not.

model:
  backend: llamacpp            # llamacpp | stub
  endpoint: "http://127.0.0.1:8080"   # llama-server on the Spark
  model_id: "qwen3-8b-q6_k"    # journal provenance string (decisions.model_id)
  temperature: 0.0             # deterministic gates deserve deterministic proposals
  max_tokens: 512
  timeout_secs: 30
  retries_on_invalid: 1        # one retry with the validation error appended,
                               # then REJECT to journal (never crash, never drop)

router:
  # priority_score = tier_weight + urgency_weight + round(novelty*4) + corroboration_bonus
  tier_weight:        # by source_tier
    1: 6
    2: 4
    3: 1
  urgency_weight:
    high: 6
    medium: 3
    low: 0
  corroboration_bonus_per_outlet: 1   # (independent_outlets - 1) * this, capped
  corroboration_bonus_cap: 3
  # queue.priority is ascending (0 = most urgent). Overnight enqueue priority:
  # max(0, overnight_base - priority_score)
  overnight_base: 50

```

## `config/a2.yaml`

```yaml
# A2 Analyst configuration (Phase 3, observe-only).
model:
  backend: llamacpp            # llamacpp | stub
  endpoint: "http://127.0.0.1:8081"   # second llama-server: Analyst slot
  model_id: "qwen3-32b-q5_k_m"
  temperature: 0.0
  max_tokens: 1200
  timeout_secs: 120            # 32B on the Spark: ~10-15 tok/s; theses are ~400 tokens
  retries_on_invalid: 1

```

## `config/c8.yaml`

```yaml
# C8 Regime Context Builder (Phase 3).
interval_market_secs: 1800     # snapshot every 30 min during RTH
interval_offhours_secs: 3600   # hourly otherwise

```

## `config/deadman.yaml`

```yaml
# Dead-man thresholds (phase4-design-v1_0 D4). RTH values; off-hours alert-only.
# Ladder: ALERT -> BLOCK_ENTRIES -> never auto-flatten.
components:
  ingestion:   {alert_min: 3,  block_entries_min: 10}
  marketdata:  {alert_min: 2,  block_entries_min: 2, exit_engine_suspend_min: 10}
  triage:      {alert_min: 5}
  analyst:     {alert_min: 5}
  gate:        {alert_min: 5}
c4:
  reconcile_interval_min: 15
  exit_unprotected_max_secs: 45      # D3 cancel-and-replace reinstatement window
  drawdown_breaker_pct: 0.02
  heartbeat_secs: 60

```

## `config/dedup.yaml`

```yaml
# C2 configuration (baseline §4 C2, v0.4 two-collection rule)
similarity_threshold: 0.90      # >= this cosine sim to an item in trailing 48h -> duplicate
cluster_threshold: 0.80         # >= this sim -> same story cluster (corroboration), below -> new cluster
dedup_window_hours: 48
prune_interval_secs: 3600       # hourly prune of the dedup_48h collection
collections:
  dedup: "dedup_48h"
  retrieval: "retrieval"        # material items only; admission = A1 flag (Phase 2 caller)
embedding_dim: 384              # bge-small-en-v1.5 and the hash test embedder both emit 384

```

## `config/exit_profiles.yaml`

```yaml
# Phase 4 exit profiles (phase4-design-v1_0 D6). Practitioner starting points,
# tuned only through the A9 loop.
profiles:
  short_term_v1:
    initial_stop:   {method: atr, k: 2.0}
    catastrophe:    {method: atr, k: 3.5}
    breakeven_at_R: 1.0
    trail:          {activate_at_R: 1.5, method: atr, k: 2.5}
    time_stop:      {window: thesis, min_progress_R: 0.5}
    realization:    {target_fraction: 0.7, action: scale_out_50}
    earnings_blackout_exit: true
    overnight_hold: eod_rule_v1
  long_term_v1:
    initial_stop:   {method: atr, k: 3.0}
    catastrophe:    {method: atr, k: 4.5}
    breakeven_at_R: 1.0
    trail:          {activate_at_R: 2.0, method: atr_weekly, k: 4.0}
    time_stop:      null
    realization:    {target_fraction: 0.7, action: review_flag}
    earnings_blackout_exit: false
    overnight_hold: default_hold
discretion_bands:
  k: [1.5, 2.5]
  realization_fraction: [0.5, 0.9]
  time_window_sessions: [1, 3]
overnight_rule:                      # D1 thresholds
  check_time_et: "15:45"
  hold_min_unrealized_R: 0.3
  young_max_age_sessions: 1
  young_max_realized_fraction: 0.5

```

## `config/gate.yaml`

```yaml
# C3 Market Confirmation Gate (Phase 3).
# ALL VALUES ARE PLACEHOLDERS pending the baseline §14 gate-threshold design
# item ("Concrete gate thresholds — initial values and tuning protocol").
# The extended-skip 6% is the one number named in the baseline itself.
gate:
  intraday_move_pct: 0.015     # X: >= +1.5% from pre-news
  intraday_vol_mult: 2.5       # Y: >= 2.5x baseline minute volume
  intraday_window_min: 30      # N: within 30 minutes of publish
  extended_pct: 0.06           # baseline v0.5: skip if >= +6% from pre-news
  open_blackout_min: 15        # no entries first 15 min after open (baseline)
  handoff_gap_ratio: 0.5       # gap >= 0.5x magnitude_est -> PRICED_IN
  impact_medium_min: 0.02      # magnitude_est buckets for credibility
  impact_high_min: 0.05
  required_outlets:            # independent outlets required: [impact][tier]
    low:    {2: 1, 3: 1}
    medium: {2: 1, 3: 2}
    high:   {2: 2, 3: 3}       # Tier-3 single-source high-impact NEVER passes alone

```

## `config/risk.yaml`

```yaml
# Phase 4 capital + hard limits (phase4-design-v1_0 D2). Percentages/caps are
# GIT-ONLY (baseline rule 25 as amended). Operational controls (kill_switch,
# trading_capital, max_trades_per_day) live in journal.control via dashboard.
capital:
  base_mode: static
  risk_per_trade_pct: 0.005
  max_position_notional_pct: 0.15
  max_portfolio_heat_pct: 0.03
  heat_split: {SHORT: 0.02, LONG: 0.01}
  max_sector_heat_pct: 0.015
  min_viable_risk_fraction: 0.5
limits:
  max_trades_per_day_default: 5      # control-table override wins if present
  adv_participation_max: 0.01
  spread_max_bps: 40
  entry_blackout_final_min: 15
model:                               # A3 bounded discretion (Analyst slot)
  backend: stub                      # llamacpp on the Spark (:8081), stub for dev
  endpoint: "http://127.0.0.1:8081"
  model_id: "qwen3-32b-q5_k_m"
  temperature: 0.0
  max_tokens: 400
  timeout_secs: 60
  retries_on_invalid: 1

```

## `config/sources.yaml`

```yaml
# C1 source registry. Trust tiers per baseline v0.2 (§4 C1):
#   1 = SEC EDGAR / exchange releases
#   2 = established wires
#   3 = aggregated / RSS
# Gap thresholds are per-source "silence means trouble" windows, split by
# market-hours vs off-hours (the firehose being quiet at 3AM Sunday is normal).

alpaca:
  enabled: true
  tier: 2                      # alpaca_benzinga wire content
  gap_threshold_market_secs: 300      # 5 min silent during RTH -> gap
  gap_threshold_offhours_secs: 7200   # 2 h silent off-hours -> gap
  reconnect_base_secs: 1
  reconnect_max_secs: 60

edgar:
  enabled: true
  tier: 1
  poll_interval_secs: 15       # well under SEC 10 req/s fair-access cap
  gap_threshold_market_secs: 900
  gap_threshold_offhours_secs: 14400
  # Event-class forms that enter the pipeline (prefix match: "8-K" admits
  # "8-K/A"). Everything else is stored to news_items as a record but never
  # enqueued (2026-07-14 fix: Form 4 / 424B2 flood was 90%+ of volume).
  triage_forms:
    - "8-K"
    - "6-K"
    - "S-1"
    - "425"
    - "SC 13D"
    - "SC 13G"
    - "SCHEDULE 13D"
    - "SCHEDULE 13G"
    - "10-K"
    - "10-Q"
  # current-events Atom feed, 8-K prioritized via channels tag
  feed_url: "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=40&output=atom"
  extra_feeds:
    - name: "all-filings"
      url: "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&company=&dateb=&owner=include&count=40&output=atom"

rss:
  enabled: true
  tier: 3
  poll_interval_secs: 60
  gap_threshold_market_secs: 1800
  gap_threshold_offhours_secs: 21600
  feeds:
    - name: "prnewswire-news"
      url: "https://www.prnewswire.com/rss/news-releases-list.rss"
    - name: "globenewswire-public"
      url: "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"
    - name: "businesswire-all"
      url: "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRWQ=="

```

## `ops/RUNBOOK.md`

```markdown
# Pipeline Runbook (Phase 4, D5)

Operator procedures for the failure modes that matter. Every scenario ends
with "how you know it worked." Commands assume the Spark, repo at
`~/pipeline`, Postgres 16 local, services under systemd. **The broker is the
source of truth; the journal is the record; positions are protected by
broker-resident catastrophe stops even when everything here is down.**

Environment used throughout:
```bash
cd ~/pipeline
export PYTHONPATH=src PIPELINE_DSN=postgresql://trader:trader_dev@127.0.0.1:5432/trading
PSQL="psql $PIPELINE_DSN"
```

---

## 1. Total Spark failure (box dead / unbootable)

Positions are safe: catastrophe stops are GTC at the broker and do not
depend on the Spark. **Do not panic-flatten from your phone.**

1. Confirm protection from any browser: Alpaca dashboard → Orders → open
   stop orders should match one per open position.
2. If you must reduce risk manually, use the Alpaca UI to place exits; the
   pipeline will reconcile them as CLOSED_EXTERNAL on next boot.
3. Recover the box (or a replacement): install Postgres 16, clone the repo,
   restore last night's dump (§3), start services (§6).
4. **Verified when:** first C4 reconciliation reports drift = adopted 0 /
   qty snaps only for anything you touched manually; `journal.health` rows
   all OK.

## 2. Broker outage (Alpaca API down, Spark fine)

- C4's periodic reconcile fails → `health.broker_api = DEGRADED`; dead-man
  keeps entries blocked if marketdata is also affected.
- Exits cannot be submitted; catastrophe stops (already resident broker-side)
  remain whatever the broker's own state is — an exchange-side outage is the
  one scenario stops can't cover. Nothing to do but watch.
- Do NOT restart services in a loop; C4 retries on its own.
- **Verified when:** `SELECT * FROM journal.health WHERE component='broker_api'`
  returns OK after the outage and the next reconcile summary shows no
  unexplained drift.

## 3. Postgres restore from backup

Nightly dumps: `~/pipeline-backups/trading-YYYYMMDD.dump` (14-day rotation,
written by `ops/backup.sh` from cron/systemd-timer).

```bash
sudo systemctl stop c4-exec a3-risk a1-triage a2-analyst c3-gate c1-ingestion
dropdb --if-exists trading && createdb trading -O trader
pg_restore -d trading --no-owner ~/pipeline-backups/trading-<DATE>.dump
$PSQL -c "SELECT count(*) FROM journal.decisions"   # sanity: non-zero
```
Start services (§6). C4's boot reconciliation will adopt/close anything that
happened at the broker after the dump was taken — read the reconcile summary
in the log before re-enabling entries.
**Verified when:** counts match expectations, reconciliation summary is
explainable, dashboard History tab renders.

## 4. Config rollback

Configs are git-versioned; every decision row carries `config_version`.
```bash
git log --oneline -- config/          # find the good version
git checkout <sha> -- config/
sudo systemctl restart a3-risk c4-exec c3-gate
$PSQL -c "SELECT config_version, registered_ts FROM journal.config_versions ORDER BY registered_ts DESC LIMIT 3"
```
**Verified when:** a new config_version row appears and new decisions
reference it.

## 5. Drawdown breaker / kill switch discipline

- Breaker trips at −2% daily on effective capital. It is **one-way**: code
  never resets it. Reset from the dashboard only after you have read the
  day's exits and understand the loss. Not before.
- Kill switch: blocks entries only. Exits and stop management continue.
- `block_entries` set by DEADMAN clears itself on heartbeat recovery; set by
  you, it stays until you clear it.

## 6. Cold start order (and the only order)

```bash
sudo systemctl start postgresql        # 1. store
sudo systemctl start qdrant            # 2. vectors (C2)
sudo systemctl start llama-a1 llama-a2 # 3. models (:8080, :8081)
sudo systemctl start c1-ingestion      # 4. feed
sudo systemctl start a1-triage a2-analyst c3-gate
sudo systemctl start a3-risk
sudo systemctl start c4-exec           # 5. LAST: reconciles before consuming
```
**Verified when:** `journal.health` shows every component OK and C4's log
prints the reconciliation summary before its first intent.

## 7. Backup verification (monthly, mandatory)

A backup that has never been restored is a hope, not a backup.
```bash
ops/backup.sh                                   # take one now
createdb trading_restore_test -O trader
pg_restore -d trading_restore_test --no-owner ~/pipeline-backups/<latest>
psql postgresql://trader:trader_dev@127.0.0.1:5432/trading_restore_test \
  -c "SELECT count(*) FROM journal.decisions" \
  -c "SELECT count(*) FROM news.news_items"
dropdb trading_restore_test
```
**Verified when:** counts match the live DB at dump time.

```

## `ops/alpaca-smoke.py`

```python
"""Alpaca paper smoke test — run ON THE SPARK, first deployment session.
Validates: auth, account/settled-cash read, positions read, a far-from-market
limit order submit + cancel (never fills), order status round-trip.
Usage: PYTHONPATH=src ALPACA_KEY_ID=... ALPACA_SECRET_KEY=... python3 ops/alpaca-smoke.py
"""
import asyncio, sys

async def main():
    from common.broker import AlpacaBroker
    b = AlpacaBroker()
    acct = await b.get_account()
    print(f"account: equity={acct.equity:.2f} settled={acct.settled_cash:.2f}")
    pos = await b.get_positions()
    print(f"positions: {len(pos)}")
    o = await b.submit_limit("AAPL", "BUY", 1, 1.00,
                             client_order_id="smoke-test-limit-1")
    print(f"submitted: {o.broker_order_id} status={o.status}")
    o2 = await b.get_order(o.broker_order_id)
    print(f"round-trip status={o2.status}")
    ok = await b.cancel(o.broker_order_id)
    print(f"cancelled: {ok}")
    final = await b.get_order(o.broker_order_id)
    assert final.status in ("canceled", "pending_cancel"), final.status
    print("SMOKE TEST PASSED")

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

```

## `ops/backup.sh`

```bash
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

```

## `ops/docker-compose.yml`

```yaml
# Dev/Spark infrastructure: PostgreSQL 16 + Qdrant server.
# On the Spark you may prefer native PG16 (baseline assumes Postgres on NVMe);
# this compose file is equivalent for development.
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: trader
      POSTGRES_PASSWORD: trader_dev        # dev only; use a real secret on the Spark
      POSTGRES_DB: trading
    ports: ["5432:5432"]
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ../schema:/docker-entrypoint-initdb.d:ro   # applies both schemas on first boot

  qdrant:
    image: qdrant/qdrant:latest
    ports: ["6333:6333"]
    volumes:
      - qdrantdata:/qdrant/storage

volumes:
  pgdata:
  qdrantdata:

```

## `ops/systemd/a1-triage.service`

```ini
[Unit]
Description=A1 Triage + Router Service (trading pipeline)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/pipeline
Environment=PYTHONPATH=/opt/pipeline/src
EnvironmentFile=/etc/pipeline/pipeline.env
ExecStart=/opt/pipeline/.venv/bin/python -m a1_triage.service
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target

```

## `ops/systemd/a2-analyst.service`

```ini
[Unit]
Description=a2-analyst Service (trading pipeline)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/pipeline
Environment=PYTHONPATH=/opt/pipeline/src
EnvironmentFile=/etc/pipeline/pipeline.env
ExecStart=/opt/pipeline/.venv/bin/python -m a2_analyst.service
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target

```

## `ops/systemd/a3-risk.service`

```ini
[Unit]
Description=Pipeline a3-risk (Phase 4)
After=network-online.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/pipeline
Environment=PYTHONPATH=/home/trader/pipeline/src
EnvironmentFile=/home/trader/pipeline/.env
ExecStart=/usr/bin/python3 -m a3_risk.service
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target

```

## `ops/systemd/c1-ingestion.service`

```ini
[Unit]
Description=C1 News Ingestion Service (trading pipeline)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/pipeline
Environment=PYTHONPATH=/opt/pipeline/src
EnvironmentFile=/etc/pipeline/pipeline.env
ExecStart=/opt/pipeline/.venv/bin/python -m c1_ingestion.service
Restart=always
RestartSec=5
# journald captures structured stdout logs
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target

```

## `ops/systemd/c2-dedup.service`

```ini
[Unit]
Description=C2 Dedup / Clustering Service (trading pipeline)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/pipeline
Environment=PYTHONPATH=/opt/pipeline/src
EnvironmentFile=/etc/pipeline/pipeline.env
ExecStart=/opt/pipeline/.venv/bin/python -m c2_dedup.service
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target

```

## `ops/systemd/c3-gate.service`

```ini
[Unit]
Description=c3-gate Service (trading pipeline)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/pipeline
Environment=PYTHONPATH=/opt/pipeline/src
EnvironmentFile=/etc/pipeline/pipeline.env
ExecStart=/opt/pipeline/.venv/bin/python -m c3_gate.service
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target

```

## `ops/systemd/c4-exec.service`

```ini
[Unit]
Description=Pipeline c4-exec (Phase 4)
After=network-online.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/pipeline
Environment=PYTHONPATH=/home/trader/pipeline/src
EnvironmentFile=/home/trader/pipeline/.env
ExecStart=/usr/bin/python3 -m c4_exec.service
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target

```

## `ops/systemd/c8-regime.service`

```ini
[Unit]
Description=c8-regime Service (trading pipeline)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/pipeline
Environment=PYTHONPATH=/opt/pipeline/src
EnvironmentFile=/etc/pipeline/pipeline.env
ExecStart=/opt/pipeline/.venv/bin/python -m c8_regime.service
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target

```

## `ops/systemd/pipeline-backup.service`

```ini
[Unit]
Description=Pipeline pg_dump backup

[Service]
Type=oneshot
User=trader
EnvironmentFile=/home/trader/pipeline/.env
ExecStart=/home/trader/pipeline/ops/backup.sh

```

## `ops/systemd/pipeline-backup.timer`

```ini
[Unit]
Description=Nightly pipeline backup

[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true

[Install]
WantedBy=timers.target

```

## `pyproject.toml`

```toml
[project]
name = "news-pipeline"
version = "0.4.5"
description = "C1+C2 ingestion/dedup, A1 triage/router, A2 analyst, C3 gate, C8 regime for the multi-agent news trading system (baseline v0.5, Phase 1)"
requires-python = ">=3.12"
dependencies = [
    "psycopg[binary,pool]>=3.1",
    "websockets>=12",
    "httpx>=0.27",
    "feedparser>=6.0",
    "pydantic>=2.7",
    "qdrant-client>=1.9",
    "pandas-market-calendars>=4.4",
]

[project.optional-dependencies]
embed = ["sentence-transformers>=3.0"]   # real embedder; install on the Spark
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.setuptools.packages.find]
where = ["src"]

```

## `schema/journal-schema.sql`

```sql
-- ============================================================================
-- Multi-Agent News Trading System — Journal / Decision-Log Schema
-- Version: 1.0   (baseline v0.5, July 2026)
-- Target:  PostgreSQL 15+  (TimescaleDB optional; see companion spec §7)
--
-- The journal is the system's institutional memory and the one irreplaceable
-- asset (baseline §11.4). Consumers: A7 EOD report, A8 briefing, A9 weekend
-- review, A11 eval, A12 guard context, C6 dashboard, C9 replay verification.
--
-- Conventions (companion spec §2):
--   * All timestamps TIMESTAMPTZ, stored UTC. ET conversion in code only.
--   * Every table carries schema_version (baseline §11.5).
--   * config_version = git commit SHA of the config repo active at write time.
--   * R = initial risk unit (entry price − initial stop) per position.
--   * Money in NUMERIC(14,4); never floats.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS journal;
SET search_path TO journal;

-- ----------------------------------------------------------------------------
-- 0. Schema metadata & config registry
-- ----------------------------------------------------------------------------

CREATE TABLE schema_meta (
  schema_version  SMALLINT PRIMARY KEY,
  applied_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  description     TEXT NOT NULL
);
INSERT INTO schema_meta VALUES (1, now(), 'Initial journal schema, baseline v0.5');

CREATE TABLE config_versions (
  config_version  TEXT PRIMARY KEY,          -- git commit SHA (config repo)
  applied_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  summary         TEXT,                      -- commit subject line
  proposal_id     BIGINT,                    -- A9 proposal that produced it (FK added below)
  schema_version  SMALLINT NOT NULL DEFAULT 1
);

-- ----------------------------------------------------------------------------
-- 1. Regime snapshots (C8) — referenced by every decision
-- ----------------------------------------------------------------------------

CREATE TABLE regime_snapshots (
  regime_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts              TIMESTAMPTZ NOT NULL,
  features        JSONB NOT NULL,            -- {index_trend, vix, vix_chg, breadth, sector_rs, ...}
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_regime_ts ON regime_snapshots (ts DESC);

-- ----------------------------------------------------------------------------
-- 2. decisions — the spine. One row per stage verdict, including every veto
--    and discard (baseline principle 5: everything is logged).
-- ----------------------------------------------------------------------------

CREATE TABLE decisions (
  decision_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- What is being decided about --------------------------------------------
  signal_id       TEXT NOT NULL,             -- unit flowing through the pipeline
  item_id         TEXT,                      -- news-store item (NULL for synthetic/scheduled)
  item_revision   SMALLINT,                  -- which revision was seen (corrections, v0.4)
  derived_from    BIGINT REFERENCES decisions(decision_id),  -- sympathy-lane parent (v0.2)
  ticker          TEXT,                      -- NULL for untagged/macro items

  -- Who decided and how ------------------------------------------------------
  stage           TEXT NOT NULL CHECK (stage IN
                    ('TRIAGE','ANALYST','GATE','RISK','ORDER','GUARD',
                     'PREMARKET','POSITION_REVIEW','SYSTEM')),
  agent           TEXT NOT NULL,             -- 'A1'..'A12','C3','C4','C7'
  action          TEXT NOT NULL,             -- ESCALATE|DISCARD|THESIS|REJECT|PASS|VETO|
                                             -- SIZED|SUBMITTED|FILLED|EXIT|HOLD|TIGHTEN_STOP|...
  veto_reason     TEXT,                      -- machine code when action='VETO'
                                             -- (GATE_NO_CONFIRM, GATE_EXTENDED, CREDIBILITY,
                                             --  SIZE_CLIPPED, HEAT_CAP, LIQUIDITY, HALTED,
                                             --  KILL_SWITCH, BREAKER, TRADES_PER_DAY, ...)

  -- Full model/gate output ---------------------------------------------------
  payload         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- entire structured output
                                             -- (A2 thesis incl. expected_move_window,
                                             --  invalidations, source_risk; C3 gate numbers;
                                             --  A3 sizing math; A12 verdict)
  reason          TEXT,                      -- human-readable reasoning snippet (tape display)
  confidence      REAL,                      -- ordinal (baseline rule 6)

  -- Provenance (replay + attribution) ---------------------------------------
  model_id        TEXT,                      -- e.g. 'qwen3-32b-q5', NULL for pure code
  latency_ms      INTEGER,
  config_version  TEXT NOT NULL REFERENCES config_versions(config_version),
  regime_id       BIGINT REFERENCES regime_snapshots(regime_id),
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_dec_ts        ON decisions (ts DESC);
CREATE INDEX idx_dec_signal    ON decisions (signal_id, ts);
CREATE INDEX idx_dec_item      ON decisions (item_id) WHERE item_id IS NOT NULL;
CREATE INDEX idx_dec_ticker_ts ON decisions (ticker, ts DESC) WHERE ticker IS NOT NULL;
CREATE INDEX idx_dec_veto      ON decisions (ts DESC) WHERE action = 'VETO';
CREATE INDEX idx_dec_stage     ON decisions (stage, ts DESC);

-- ----------------------------------------------------------------------------
-- 3. intents & orders & fills — A3 output through C4's order state machine
-- ----------------------------------------------------------------------------

CREATE TABLE intents (
  intent_id       TEXT PRIMARY KEY,          -- idempotency key (v0.4): duplicates are no-ops
  decision_id     BIGINT NOT NULL REFERENCES decisions(decision_id),
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  ticker          TEXT NOT NULL,
  side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),  -- SELL = exits only (long-only)
  qty             INTEGER NOT NULL CHECK (qty > 0),
  limit_price     NUMERIC(14,4) NOT NULL,    -- limit orders only (rule 11)
  gate_snapshot   JSONB,                     -- C3 price/volume snapshot the limit was priced off
  exit_policy     JSONB,                     -- full v0.3 exit_policy object (entries)
  horizon         TEXT CHECK (horizon IN ('SHORT','LONG')),
  effective_capital NUMERIC(14,4),           -- min(broker_equity, trading_capital) at sizing (v0.5)
  risk_budget     NUMERIC(14,4),             -- $ risked = risk_per_trade_pct * effective_capital
  status          TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING','SUBMITTED','REJECTED','FILLED',
                                      'PARTIAL','CANCELLED','EXPIRED')),
  config_version  TEXT NOT NULL REFERENCES config_versions(config_version),
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_intents_ticker ON intents (ticker, ts DESC);

CREATE TABLE orders (
  order_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  intent_id       TEXT REFERENCES intents(intent_id),     -- NULL for broker-side catastrophe stops
  position_id     BIGINT,                                  -- FK added after positions
  broker_order_id TEXT UNIQUE,
  order_role      TEXT NOT NULL CHECK (order_role IN
                    ('ENTRY','EXIT','CATASTROPHE_STOP','SCALE_OUT','FLATTEN')),
  state           TEXT NOT NULL CHECK (state IN
                    ('NEW','ACCEPTED','PARTIAL','FILLED','CANCELLED','REJECTED','EXPIRED','HELD_HALT')),
  qty             INTEGER NOT NULL,
  limit_price     NUMERIC(14,4),
  stop_price      NUMERIC(14,4),
  submitted_ts    TIMESTAMPTZ,
  closed_ts       TIMESTAMPTZ,
  raw             JSONB,                     -- last broker payload (reconciliation evidence)
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_orders_intent ON orders (intent_id);

CREATE TABLE fills (
  fill_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id        BIGINT NOT NULL REFERENCES orders(order_id),
  ts              TIMESTAMPTZ NOT NULL,
  qty             INTEGER NOT NULL,
  price           NUMERIC(14,4) NOT NULL,
  fees            NUMERIC(14,4) NOT NULL DEFAULT 0,
  broker_exec_id  TEXT UNIQUE,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_fills_order ON fills (order_id);

-- ----------------------------------------------------------------------------
-- 4. positions & the exit-policy state machine (v0.3 §5)
-- ----------------------------------------------------------------------------

CREATE TABLE positions (
  position_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ticker          TEXT NOT NULL,
  horizon         TEXT NOT NULL CHECK (horizon IN ('SHORT','LONG')),
  profile         TEXT NOT NULL,             -- 'short_term_v1' | 'long_term_v1' | ...
  status          TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')),

  opened_ts       TIMESTAMPTZ NOT NULL,
  closed_ts       TIMESTAMPTZ,

  entry_intent_id TEXT NOT NULL REFERENCES intents(intent_id),
  thesis_decision_id BIGINT NOT NULL REFERENCES decisions(decision_id),  -- the A2 thesis
  item_id         TEXT,                      -- originating news item (dashboard drill-down)

  qty_initial     INTEGER NOT NULL,
  qty_open        INTEGER NOT NULL,          -- decremented by scale-outs
  avg_entry       NUMERIC(14,4) NOT NULL,
  initial_stop    NUMERIC(14,4) NOT NULL,    -- defines R: r_unit = avg_entry - initial_stop
  r_unit          NUMERIC(14,4) NOT NULL CHECK (r_unit > 0),

  exit_policy     JSONB NOT NULL,            -- CURRENT policy state (stops move; history below)
  catastrophe_stop_order_id BIGINT REFERENCES orders(order_id),  -- broker-resident tier (v0.4)

  -- C4 mark-to-market cache (dashboard reads; refreshed on bar close) --------
  last_price      NUMERIC(14,4),
  last_price_ts   TIMESTAMPTZ,

  realized_pnl    NUMERIC(14,4) NOT NULL DEFAULT 0,   -- accumulated over partial exits
  config_version  TEXT NOT NULL REFERENCES config_versions(config_version),
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_pos_status ON positions (status, opened_ts DESC);
CREATE INDEX idx_pos_ticker ON positions (ticker) WHERE status = 'OPEN';
ALTER TABLE orders ADD CONSTRAINT fk_orders_position
  FOREIGN KEY (position_id) REFERENCES positions(position_id);

-- Exit-policy state history: every mutation of every exit layer, forever.
CREATE TABLE position_events (
  event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  position_id     BIGINT NOT NULL REFERENCES positions(position_id),
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  event_type      TEXT NOT NULL CHECK (event_type IN
                    ('STOPS_PLACED','BREAKEVEN_MOVED','TRAIL_UPDATED','STOP_TIGHTENED',
                     'TIME_STOP_ARMED','INVALIDATION_ARMED','INVALIDATION_FIRED',
                     'EARNINGS_BLACKOUT_FLAGGED','OVERNIGHT_HOLD_DECISION',
                     'HALT_FROZEN','HALT_RESUMED','SCALE_OUT','EXIT','GUARD_ACTION',
                     'CORPORATE_ACTION_ADJ','RECONCILED')),
  actor           TEXT NOT NULL,             -- 'C4','A12','A6','OPERATOR','BROKER'
  old_value       JSONB,
  new_value       JSONB,
  r_progress      NUMERIC(8,3),              -- unrealized R at event time
  detail          TEXT,
  decision_id     BIGINT REFERENCES decisions(decision_id),  -- model decision that caused it
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_pev_position ON position_events (position_id, ts);

-- Per-exit attribution: one row per exit execution, INCLUDING partials (v0.3 L4).
CREATE TABLE exits (
  exit_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  position_id     BIGINT NOT NULL REFERENCES positions(position_id),
  order_id        BIGINT REFERENCES orders(order_id),
  ts              TIMESTAMPTZ NOT NULL,
  exit_layer      TEXT NOT NULL CHECK (exit_layer IN
                    ('STOP','CATASTROPHE','BREAKEVEN','TRAIL','TIME','TARGET',
                     'INVALIDATION','GUARD','REVIEW','EARNINGS','OVERNIGHT',
                     'BREAKER','KILL','OPERATOR')),
  qty             INTEGER NOT NULL,
  price           NUMERIC(14,4) NOT NULL,
  realized_pnl    NUMERIC(14,4) NOT NULL,
  r_multiple      NUMERIC(8,3) NOT NULL,     -- realized_pnl / (r_unit * qty)
  is_partial      BOOLEAN NOT NULL DEFAULT FALSE,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_exits_position ON exits (position_id);
CREATE INDEX idx_exits_layer_ts ON exits (exit_layer, ts DESC);

-- ----------------------------------------------------------------------------
-- 5. Guard ledger (A12) — verdicts now, outcome classification later (A11)
-- ----------------------------------------------------------------------------

CREATE TABLE guard_ledger (
  guard_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  decision_id     BIGINT NOT NULL REFERENCES decisions(decision_id),
  position_id     BIGINT NOT NULL REFERENCES positions(position_id),
  item_id         TEXT NOT NULL,
  ts              TIMESTAMPTZ NOT NULL,
  thesis_intact   BOOLEAN NOT NULL,
  recommended_action TEXT NOT NULL CHECK (recommended_action IN ('HOLD','TIGHTEN_STOP','EXIT')),
  urgency         TEXT,
  auto_executed   BOOLEAN NOT NULL DEFAULT FALSE,       -- config-gated (rule 12)
  action_taken    TEXT,                                  -- what actually happened
  outcome_class   TEXT CHECK (outcome_class IN ('SAVE','SHAKEOUT','NEUTRAL')),  -- A11, later
  outcome_pnl_r   NUMERIC(8,3),                          -- counterfactual delta in R
  classified_ts   TIMESTAMPTZ,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_guard_position ON guard_ledger (position_id);
CREATE INDEX idx_guard_pending  ON guard_ledger (ts) WHERE outcome_class IS NULL;

-- ----------------------------------------------------------------------------
-- 6. A11 measurement layer (v0.3 exit metrics + counterfactuals)
-- ----------------------------------------------------------------------------

CREATE TABLE trade_metrics (            -- one row per CLOSED position, written by A11 nightly
  position_id     BIGINT PRIMARY KEY REFERENCES positions(position_id),
  computed_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
  holding_seconds BIGINT NOT NULL,
  mae_r           NUMERIC(8,3) NOT NULL,     -- max adverse excursion, in R
  mfe_r           NUMERIC(8,3) NOT NULL,     -- max favorable excursion, in R
  realized_r      NUMERIC(8,3) NOT NULL,
  exit_efficiency NUMERIC(6,4),              -- realized_pnl / MFE$ (NULL if MFE<=0)
  magnitude_predicted NUMERIC(8,4),          -- from A2 thesis payload
  magnitude_realized  NUMERIC(8,4),
  window_hit      BOOLEAN,                   -- reached min progress inside expected_move_window?
  schema_version  SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE counterfactuals (          -- post-exit and post-veto price paths, recorded by code
  cf_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN ('POST_EXIT','VETOED_TRADE','GUARD_CF')),
  exit_id         BIGINT REFERENCES exits(exit_id),
  decision_id     BIGINT REFERENCES decisions(decision_id),   -- the veto, for VETOED_TRADE
  ticker          TEXT NOT NULL,
  anchor_ts       TIMESTAMPTZ NOT NULL,      -- exit time / hypothetical entry time
  anchor_price    NUMERIC(14,4) NOT NULL,
  horizon_desc    TEXT NOT NULL,             -- e.g. '1R_equivalent', '2_sessions'
  path            JSONB NOT NULL,            -- [[ts,price],...] downsampled
  outcome_r       NUMERIC(8,3),              -- foregone/avoided result in R terms
  computed_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  CHECK (exit_id IS NOT NULL OR decision_id IS NOT NULL)
);
CREATE INDEX idx_cf_kind ON counterfactuals (kind, anchor_ts DESC);

CREATE TABLE metric_rollups (           -- A11 nightly/weekly aggregates that A9 and A7 read
  rollup_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  period_start    DATE NOT NULL,
  granularity     TEXT NOT NULL CHECK (granularity IN ('DAY','WEEK')),
  metric          TEXT NOT NULL,             -- 'triage_escalation_rate','gate_pass_rate',
                                             -- 'exit_efficiency:TRAIL','guard_save_rate',
                                             -- 'veto_counterfactual_pnl_r', ...
  value           NUMERIC(16,6),
  breakdown       JSONB,                     -- per-ticker/-profile/-regime slices
  config_version  TEXT REFERENCES config_versions(config_version),
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (period_start, granularity, metric)
);

-- ----------------------------------------------------------------------------
-- 7. Governance: A9 proposals with their attribution loop (baseline §8)
-- ----------------------------------------------------------------------------

CREATE TABLE proposals (
  proposal_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  author          TEXT NOT NULL DEFAULT 'A9',
  title           TEXT NOT NULL,
  current_state   TEXT NOT NULL,             -- parameter/prompt as-is
  proposed_diff   TEXT NOT NULL,
  evidence        JSONB NOT NULL,            -- {decision_ids:[], rollup_ids:[], n_instances:int}
  expected_effect TEXT NOT NULL,
  success_metric  TEXT NOT NULL,             -- metric name + target, evaluated next weekend
  status          TEXT NOT NULL DEFAULT 'PROPOSED'
                    CHECK (status IN ('PROPOSED','APPROVED','REJECTED','SHADOW','EVALUATED')),
  reviewed_ts     TIMESTAMPTZ,
  config_version_result TEXT REFERENCES config_versions(config_version),  -- commit if approved
  evaluation      JSONB,                     -- next weekend's verdict vs success_metric
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
ALTER TABLE config_versions ADD CONSTRAINT fk_cfg_proposal
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id);

-- ----------------------------------------------------------------------------
-- 8. Operational controls, audit, health, outbox (v0.5 / C5 / C6 / C7)
-- ----------------------------------------------------------------------------

CREATE TABLE control (
  key             TEXT PRIMARY KEY,          -- 'kill_switch','drawdown_breaker','trading_capital'
  value           TEXT NOT NULL,
  updated_ts      TIMESTAMPTZ,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
INSERT INTO control (key, value) VALUES
  ('kill_switch','0'), ('drawdown_breaker','0'), ('trading_capital','50000');

CREATE TABLE audit (
  audit_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor           TEXT NOT NULL,             -- dashboard user, 'C4', 'C7'
  action          TEXT NOT NULL,             -- KILL_SWITCH_ON/OFF, CAPITAL_SET, BREAKER_TRIP, ...
  old_value       TEXT,
  new_value       TEXT,
  detail          TEXT,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_audit_ts ON audit (ts DESC);

CREATE TABLE health (
  component       TEXT PRIMARY KEY,          -- 'ingestion','triage_model','analyst_model',
                                             -- 'broker_api','scheduler','backup'
  status          TEXT NOT NULL CHECK (status IN ('OK','DEGRADED','DOWN')),
  detail          TEXT,
  updated_ts      TIMESTAMPTZ NOT NULL,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE outbox (                    -- A7/A8 write; C5 mailer sends (no agent has SMTP)
  message_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind            TEXT NOT NULL CHECK (kind IN ('EOD_REPORT','MORNING_BRIEFING','ALERT')),
  subject         TEXT NOT NULL,
  body            TEXT NOT NULL,             -- rendered; numbers computed by code (rule 5)
  fact_sheet      JSONB,                     -- the code-computed numbers the narrative was given
  status          TEXT NOT NULL DEFAULT 'QUEUED'
                    CHECK (status IN ('QUEUED','SENT','FAILED')),
  sent_ts         TIMESTAMPTZ,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_outbox_queued ON outbox (created_ts) WHERE status = 'QUEUED';

-- ============================================================================
-- 9. C6 dashboard views — the exact read shapes from c6-dashboard-spec v1.2 §6.
--    The reference implementation binds to these names/columns unchanged.
-- ============================================================================

CREATE VIEW dash_decisions AS
SELECT decision_id                              AS id,
       EXTRACT(EPOCH FROM ts)                   AS ts,
       item_id,
       stage,
       ticker,
       action,
       COALESCE(reason, veto_reason)            AS detail,
       latency_ms
FROM decisions
ORDER BY decision_id DESC;

CREATE VIEW dash_positions AS
SELECT p.position_id                            AS id,
       p.ticker,
       p.qty_open                               AS qty,
       p.avg_entry                              AS entry_price,
       COALESCE(p.last_price, p.avg_entry)      AS current_price,
       (p.exit_policy->'initial_stop'->>'price')::numeric AS stop_price,
       (p.exit_policy->'realization'->>'price')::numeric  AS target_price,
       EXTRACT(EPOCH FROM p.opened_ts)          AS opened_ts,
       EXTRACT(EPOCH FROM p.closed_ts)          AS closed_ts,
       p.status,
       (SELECT e.exit_layer FROM exits e WHERE e.position_id = p.position_id
        ORDER BY e.ts DESC LIMIT 1)             AS exit_reason,
       p.realized_pnl,
       LEFT(d.reason, 200)                      AS thesis,
       p.item_id
FROM positions p
JOIN decisions d ON d.decision_id = p.thesis_decision_id;

CREATE VIEW dash_health  AS SELECT component, status, detail,
       EXTRACT(EPOCH FROM updated_ts) AS updated_ts FROM health;
CREATE VIEW dash_control AS SELECT key, value,
       EXTRACT(EPOCH FROM updated_ts) AS updated_ts FROM control;
CREATE VIEW dash_audit   AS SELECT audit_id AS id, EXTRACT(EPOCH FROM ts) AS ts,
       actor, action, COALESCE(old_value||' -> '||new_value, detail) AS detail FROM audit;

-- ============================================================================
-- End of schema v1. Migration policy: additive changes bump schema_version
-- DEFAULT and append to schema_meta; destructive changes forbidden (A9 must
-- read last month's rows — baseline §11.5).
-- ============================================================================

```

## `schema/news-store-schema.sql`

```sql
-- ============================================================================
-- Multi-Agent News Trading System — News Store + Queue Schema
-- Version: 1.0   (baseline v0.5, July 2026)
-- Target:  PostgreSQL 15+
--
-- Phase 1 artifact. Three concerns in one schema:
--   news.*   — normalized news items (revisable), dedup clusters, quarantine,
--              symbol lifecycle, ingestion gap log
--   queue.*  — the Postgres-backed message queues connecting pipeline stages
--              (at-least-once, SKIP LOCKED, dedup on dedup_key — rule 19)
--
-- Companion: queue-contracts-spec.md (message JSON contracts per hop).
-- Conventions match the journal schema: TIMESTAMPTZ/UTC, schema_version
-- everywhere, TEXT + CHECK instead of enums, money/none here.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS news;
CREATE SCHEMA IF NOT EXISTS queue;

-- ============================================================================
-- NEWS SCHEMA
-- ============================================================================
SET search_path TO news;

-- ----------------------------------------------------------------------------
-- 1. news_items — the normalized item, REVISABLE (v0.4 corrections).
--    Composite PK (item_id, revision): a correction is a new row, same item_id.
-- ----------------------------------------------------------------------------
CREATE TABLE news_items (
  item_id         TEXT NOT NULL,             -- source-scoped stable id (e.g. 'alpaca:40892639')
  revision        SMALLINT NOT NULL DEFAULT 1,
  is_correction   BOOLEAN NOT NULL DEFAULT FALSE,
  supersedes      SMALLINT,                  -- revision this one corrects (NULL for rev 1)

  -- Source & trust -----------------------------------------------------------
  source          TEXT NOT NULL,             -- 'alpaca_benzinga','polygon','edgar','rss:<feed>'
  source_tier     SMALLINT NOT NULL CHECK (source_tier IN (1,2,3)),  -- v0.2 trust tiers
  source_url      TEXT,
  author          TEXT,

  -- Content -------------------------------------------------------------------
  headline        TEXT NOT NULL,
  summary         TEXT,
  content_hash    TEXT NOT NULL,             -- sha256 of normalized headline+summary+body
  raw             JSONB,                     -- original payload (hot tier; demoted per §11.3)
  body_ref        TEXT,                      -- object-store key once demoted (raw set NULL)

  -- Symbols (OPTIONAL by design — v0.2; A1 infers for untagged items) ---------
  symbols         TEXT[] NOT NULL DEFAULT '{}',
  channels        TEXT[] NOT NULL DEFAULT '{}',   -- feed-provided tags ('earnings','m&a',...)
  lang            TEXT NOT NULL DEFAULT 'en',

  -- Time discipline (§11.5 — both clocks are load-bearing) --------------------
  published_ts    TIMESTAMPTZ NOT NULL,      -- the SOURCE's claimed publication time
  received_ts     TIMESTAMPTZ NOT NULL,      -- OUR wall clock at ingestion
                                             -- (replay ordering + lookahead-bias guard:
                                             --  nothing may act on an item before received_ts)
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  PRIMARY KEY (item_id, revision)
);
CREATE INDEX idx_items_received ON news_items (received_ts DESC);
CREATE INDEX idx_items_symbols  ON news_items USING GIN (symbols);
CREATE INDEX idx_items_hash     ON news_items (content_hash);
CREATE INDEX idx_items_source   ON news_items (source, received_ts DESC);

-- Latest revision per item (what most readers want)
CREATE VIEW news_items_latest AS
SELECT DISTINCT ON (item_id) *
FROM news_items
ORDER BY item_id, revision DESC;

-- ----------------------------------------------------------------------------
-- 2. clusters — C2's story grouping. Embeddings live in the vector store;
--    Postgres holds membership + the corroboration count C3 consumes (v0.2).
-- ----------------------------------------------------------------------------
CREATE TABLE clusters (
  cluster_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  canonical_item  TEXT NOT NULL,             -- first item_id seen for the story
  schema_version  SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE cluster_members (
  cluster_id      BIGINT NOT NULL REFERENCES clusters(cluster_id),
  item_id         TEXT NOT NULL,
  revision        SMALLINT NOT NULL,
  source          TEXT NOT NULL,             -- denormalized for the outlet count
  similarity      REAL,                      -- cosine sim to canonical at admission
  added_ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  PRIMARY KEY (cluster_id, item_id, revision),
  FOREIGN KEY (item_id, revision) REFERENCES news_items(item_id, revision)
);
CREATE INDEX idx_cm_item ON cluster_members (item_id);

-- Corroboration = count of INDEPENDENT outlets in the cluster (C3 credibility rule)
CREATE VIEW cluster_corroboration AS
SELECT cluster_id,
       COUNT(DISTINCT source)                    AS independent_outlets,
       COUNT(*)                                  AS total_items,
       MIN(added_ts)                             AS first_seen,
       MAX(added_ts)                             AS last_seen
FROM cluster_members
GROUP BY cluster_id;

-- ----------------------------------------------------------------------------
-- 3. quarantine — malformed input is kept, never dropped (v0.4).
--    C7 alerts on rate spikes; that is how a silently changed feed is found.
-- ----------------------------------------------------------------------------
CREATE TABLE quarantine (
  quarantine_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  received_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
  source          TEXT NOT NULL,
  reason_code     TEXT NOT NULL CHECK (reason_code IN
                    ('UNPARSEABLE_JSON','BAD_TIMESTAMP','MISSING_REQUIRED_FIELD',
                     'UNKNOWN_SCHEMA','OVERSIZE','DUPLICATE_CONFLICT','SYMBOL_UNKNOWN',
                     'ENCODING_ERROR','OTHER')),
  detail          TEXT,
  raw             JSONB,                     -- best-effort capture; TEXT dump if not JSON
  raw_text        TEXT,
  reviewed        BOOLEAN NOT NULL DEFAULT FALSE,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_quarantine_ts ON quarantine (received_ts DESC) WHERE NOT reviewed;

-- ----------------------------------------------------------------------------
-- 4. symbol_map + corporate_actions — ticker lifecycle (v0.4).
--    Effective-dated: joins pick the mapping valid at event time.
-- ----------------------------------------------------------------------------
CREATE TABLE symbol_map (
  map_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  symbol          TEXT NOT NULL,
  entity_id       TEXT NOT NULL,             -- stable company identifier (CIK preferred)
  entity_name     TEXT NOT NULL,
  effective_from  DATE NOT NULL,
  effective_to    DATE,                      -- NULL = current
  reason          TEXT NOT NULL DEFAULT 'LISTING'
                    CHECK (reason IN ('LISTING','RENAME','MERGER','SPINOFF','DELISTING')),
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_symmap_symbol ON symbol_map (symbol, effective_from);
CREATE INDEX idx_symmap_entity ON symbol_map (entity_id);

CREATE TABLE corporate_actions (
  action_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  symbol          TEXT NOT NULL,
  action_type     TEXT NOT NULL CHECK (action_type IN
                    ('SPLIT','REVERSE_SPLIT','SYMBOL_CHANGE','DIVIDEND_SPECIAL',
                     'MERGER','DELISTING')),
  ex_date         DATE NOT NULL,
  ratio           NUMERIC(12,6),             -- e.g. 10 for 10:1 split
  new_symbol      TEXT,                      -- for SYMBOL_CHANGE / MERGER
  raw             JSONB,
  applied_positions BOOLEAN NOT NULL DEFAULT FALSE,  -- C4 adjusted position state?
  applied_bars      BOOLEAN NOT NULL DEFAULT FALSE,  -- bar store adjusted?
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_ca_exdate ON corporate_actions (ex_date DESC);

-- ----------------------------------------------------------------------------
-- 5. ingestion_gaps — explicit gap log (C1 reliability; surfaced to A4/A8)
-- ----------------------------------------------------------------------------
CREATE TABLE ingestion_gaps (
  gap_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source          TEXT NOT NULL,
  gap_start       TIMESTAMPTZ NOT NULL,
  gap_end         TIMESTAMPTZ,               -- NULL while ongoing
  detected_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
  detail          TEXT,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);

-- ============================================================================
-- QUEUE SCHEMA — Postgres-backed queues (single-host; one fewer moving part
-- than Redis; LISTEN/NOTIFY wakes consumers; SKIP LOCKED makes claims safe).
-- Semantics: at-least-once delivery + consumer dedup on dedup_key (rule 19).
-- ============================================================================
SET search_path TO queue;

CREATE TABLE messages (
  msg_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  queue_name      TEXT NOT NULL,             -- see queue-contracts-spec §2 for the registry
  dedup_key       TEXT NOT NULL,             -- e.g. 'item-777:2' — consumer-side idempotency
  priority        SMALLINT NOT NULL DEFAULT 100,   -- lower = sooner (A12 path uses 0)
  payload         JSONB NOT NULL,            -- the contract body (spec §3)
  schema_version  SMALLINT NOT NULL DEFAULT 1,

  enqueued_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
  available_ts    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- delayed delivery (open handoff at 9:30)
  claimed_by      TEXT,
  claimed_ts      TIMESTAMPTZ,
  done_ts         TIMESTAMPTZ,
  attempts        SMALLINT NOT NULL DEFAULT 0,
  max_attempts    SMALLINT NOT NULL DEFAULT 5,
  last_error      TEXT,
  UNIQUE (queue_name, dedup_key)             -- duplicate enqueue = no-op (ON CONFLICT DO NOTHING)
);
CREATE INDEX idx_q_ready ON messages (queue_name, priority, available_ts)
  WHERE done_ts IS NULL AND claimed_ts IS NULL;
CREATE INDEX idx_q_claimed ON messages (queue_name, claimed_ts)
  WHERE done_ts IS NULL AND claimed_ts IS NOT NULL;

-- Claim the next ready message (safe under concurrency via SKIP LOCKED).
CREATE OR REPLACE FUNCTION claim_next(p_queue TEXT, p_consumer TEXT)
RETURNS SETOF messages LANGUAGE plpgsql AS $$
BEGIN
  RETURN QUERY
  UPDATE messages m
  SET claimed_by = p_consumer, claimed_ts = now(), attempts = m.attempts + 1
  WHERE m.msg_id = (
    SELECT msg_id FROM messages
    WHERE queue_name = p_queue AND done_ts IS NULL AND claimed_ts IS NULL
      AND available_ts <= now()
    ORDER BY priority, available_ts
    LIMIT 1
    FOR UPDATE SKIP LOCKED
  )
  RETURNING m.*;
END $$;

-- Ack / fail helpers. Failure past max_attempts routes to news.quarantine
-- (the pipeline's dead-letter destination) and marks the message done.
CREATE OR REPLACE FUNCTION ack(p_msg_id BIGINT)
RETURNS void LANGUAGE sql AS
$$ UPDATE messages SET done_ts = now() WHERE msg_id = p_msg_id $$;

CREATE OR REPLACE FUNCTION fail(p_msg_id BIGINT, p_error TEXT)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE m messages;
BEGIN
  SELECT * INTO m FROM messages WHERE msg_id = p_msg_id;
  IF m.attempts >= m.max_attempts THEN
    INSERT INTO news.quarantine (source, reason_code, detail, raw)
    VALUES ('queue:' || m.queue_name, 'OTHER',
            'DLQ after ' || m.attempts || ' attempts: ' || p_error, m.payload);
    UPDATE messages SET done_ts = now(), last_error = p_error WHERE msg_id = p_msg_id;
  ELSE
    UPDATE messages
    SET claimed_by = NULL, claimed_ts = NULL, last_error = p_error,
        available_ts = now() + (interval '5 seconds' * attempts)   -- linear backoff
    WHERE msg_id = p_msg_id;
  END IF;
END $$;

-- Reaper: reclaim messages whose consumer died mid-claim (C7 runs periodically).
CREATE OR REPLACE FUNCTION reap_stale(p_queue TEXT, p_timeout INTERVAL)
RETURNS INTEGER LANGUAGE sql AS $$
  WITH r AS (
    UPDATE messages SET claimed_by = NULL, claimed_ts = NULL
    WHERE queue_name = p_queue AND done_ts IS NULL
      AND claimed_ts IS NOT NULL AND claimed_ts < now() - p_timeout
    RETURNING 1)
  SELECT COALESCE(count(*), 0)::integer FROM r
$$;

-- ============================================================================
-- End. Migration policy identical to the journal schema: additive only.
-- ============================================================================

```

## `src/a1_triage/__init__.py`

```python

```

## `src/a1_triage/backends.py`

```python
"""Model backends for A1. The pipeline sees one interface; the model behind it
is a config line.

LlamaCppBackend: llama-server's OpenAI-compatible /v1/chat/completions with
  response_format json_schema — the grammar constraint is enforced server-side
  during decoding, so off-contract tokens can't be sampled. Code-side
  validation still runs (spec §13). Smoke-tested on the Spark, not here (no
  model server in the build environment).

StubBackend: scripted responses for tests and Spark-less dev. Deterministic:
  pops from a queue of canned responses, or applies a simple keyword rule.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from common.log import get_logger, kv

log = get_logger("a1.backend")


@dataclass
class ModelReply:
    text: str
    latency_ms: int
    model_id: str


class ModelBackend(Protocol):
    model_id: str
    async def complete(self, messages: list[dict], json_schema: dict) -> ModelReply: ...


class LlamaCppBackend:
    def __init__(self, cfg: dict):
        self.endpoint = cfg["endpoint"].rstrip("/")
        self.model_id = cfg.get("model_id", "unknown")
        self.temperature = float(cfg.get("temperature", 0.0))
        self.max_tokens = int(cfg.get("max_tokens", 512))
        self.timeout = float(cfg.get("timeout_secs", 30))

    async def complete(self, messages: list[dict], json_schema: dict) -> ModelReply:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.endpoint}/v1/chat/completions",
                json={
                    "model": self.model_id,
                    "messages": messages,
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "triage", "strict": True,
                                        "schema": json_schema},
                    },
                })
            resp.raise_for_status()
            data = resp.json()
        latency = int((time.monotonic() - t0) * 1000)
        text = data["choices"][0]["message"]["content"]
        return ModelReply(text=text, latency_ms=latency, model_id=self.model_id)


class StubBackend:
    """Test/dev backend. Two modes:
    * scripted: pass a list of raw response strings, popped in order;
    * rule-based fallback: material iff the headline contains a trigger word,
      first feed symbol as ticker — enough to drive the pipeline end-to-end.
    """
    TRIGGERS = ("acquisition", "merger", "fda", "earnings", "guidance",
                "buyback", "bankruptcy", "recall", "contract", "resigns")

    def __init__(self, scripted: list[str] | None = None,
                 model_id: str = "stub-0"):
        self.scripted = list(scripted or [])
        self.model_id = model_id
        self.calls: list[list[dict]] = []       # recorded for test assertions

    async def complete(self, messages: list[dict], json_schema: dict) -> ModelReply:
        self.calls.append(messages)
        if self.scripted:
            return ModelReply(self.scripted.pop(0), latency_ms=1, model_id=self.model_id)
        item = json.loads(messages[-1]["content"].split("\n\n")[0])
        headline = (item.get("headline") or "").lower()
        material = any(t in headline for t in self.TRIGGERS)
        out = {
            "material": material,
            "tickers": item.get("symbols", [])[:1] if material else [],
            "direction_hint": "unclear",
            "urgency": "medium" if material else "low",
            "novelty_score": 0.8 if item.get("is_new_story") else 0.3,
            "reason": "stub rule: trigger word match" if material else "stub rule: no trigger",
        }
        return ModelReply(json.dumps(out), latency_ms=1, model_id=self.model_id)


def get_backend(cfg: dict) -> ModelBackend:
    kind = cfg.get("backend", "llamacpp")
    if kind == "llamacpp":
        return LlamaCppBackend(cfg)
    if kind == "stub":
        return StubBackend()
    raise RuntimeError(f"unknown model backend: {kind!r}")

```

## `src/a1_triage/prompt.py`

```python
"""A1 triage prompt. The doctrine (agreed Phase 2 design):

Material = plausibly moves a specific US-listed equity >=2% within days.
Path A discipline: false negatives are cheaper than false positives — when in
doubt, not material. A1 is a filter, not an analyst: it does NOT estimate
magnitude, does NOT assess credibility (C3's job), does NOT decide horizon
(A2's job). It answers: is this the kind of event that moves a stock, which
tickers, which direction on its face, how urgent, how novel.
"""
from __future__ import annotations

import json

SYSTEM_PROMPT = """\
You are the triage filter in a news-driven US equities trading pipeline. For
each news item you receive, decide whether it is MATERIAL: plausibly capable
of moving a specific US-listed stock at least 2% within days.

MATERIAL (examples): earnings surprises or guidance changes; M&A activity or
credible strategic-alternative reports; FDA / regulatory decisions and major
trial results; major contract wins or losses; unexpected executive departures;
credit-rating actions; buybacks or dividends materially changed; significant
litigation outcomes; supply-chain disruptions naming specific companies;
activist stakes; 8-K filings with substantive items.

NOT MATERIAL (examples): routine product PR and minor version releases;
conference-attendance and award announcements; analyst-day recaps without new
guidance; listicles and market-recap roundups; macro commentary without a
specific equity; crypto/forex-only news; items about non-US-listed companies
with no US-listed affiliate.

Discipline: when in doubt, material=false. A missed marginal story costs
little; a false alarm wastes downstream analysis. Do not speculate beyond the
text given.

Fields:
- material: boolean per the above.
- tickers: US-listed symbols this DIRECTLY concerns. Include feed-tagged
  symbols you agree with, add obvious ones the text names (e.g. "Apple" ->
  AAPL). Leave empty if none is clearly identifiable — do NOT guess.
- direction_hint: "up", "down", or "unclear" — the face-value read for the
  primary ticker. Not a prediction; a reading of the text.
- urgency: "high" = market reaction likely within hours (M&A, FDA, earnings
  out now); "medium" = within days; "low" = slow-burn or uncertain timing.
- novelty_score: 0.0-1.0. 1.0 = first report of a new event; 0.5 = meaningful
  development of a known story; 0.1 = rehash of widely known information.
- reason: one or two sentences, plain language, why material or not.

Respond with ONLY a JSON object matching the required schema."""


FEW_SHOT: list[tuple[dict, dict]] = [
    (
        {"headline": "Acme Corp receives unsolicited acquisition proposal at $45/share",
         "summary": "Board confirms receipt; no decision made.",
         "source": "alpaca_benzinga", "source_tier": 2, "symbols": ["ACME"],
         "channels": [], "is_new_story": True, "independent_outlets": 1},
        {"material": True, "tickers": ["ACME"], "direction_hint": "up",
         "urgency": "high", "novelty_score": 1.0,
         "reason": "Confirmed takeover approach at a specific price is a classic multi-percent mover."},
    ),
    (
        {"headline": "TechWave named a Leader in industry analyst quadrant for cloud tools",
         "summary": "Company celebrates third consecutive year of recognition.",
         "source": "rss:prnewswire-news", "source_tier": 3, "symbols": [],
         "channels": [], "is_new_story": True, "independent_outlets": 1},
        {"material": False, "tickers": [], "direction_hint": "unclear",
         "urgency": "low", "novelty_score": 0.2,
         "reason": "Routine analyst-recognition PR; no earnings, guidance, or event impact."},
    ),
    (
        {"headline": "8-K - ZENITH PHARMA INC (0001234567) (Filer)",
         "summary": "Item 8.01 Other Events: FDA complete response letter received for ZP-401.",
         "source": "edgar", "source_tier": 1, "symbols": [],
         "channels": ["filing", "form:8-K", "8-K"], "is_new_story": True,
         "independent_outlets": 1},
        {"material": True, "tickers": [], "direction_hint": "down",
         "urgency": "high", "novelty_score": 1.0,
         "reason": "CRL on a pipeline drug is a major negative catalyst; ticker not stated in filing text."},
    ),
]


def render_item(item: dict, cluster: dict) -> str:
    """The user-turn content: item facts + cluster context, compact JSON.
    Synthetic (sympathy-lane) signals add a 'sympathy' block: A1 judges
    materiality FOR THAT TICKER given the parent item and stated relation."""
    payload = {
        "headline": item.get("headline"),
        "summary": item.get("summary"),
        "source": item.get("source"),
        "source_tier": item.get("source_tier"),
        "symbols": item.get("symbols", []),
        "channels": item.get("channels", []),
        "is_correction": item.get("is_correction", False),
        "is_new_story": cluster.get("is_new_story"),
        "independent_outlets": cluster.get("independent_outlets"),
    }
    if item.get("sympathy"):
        payload["sympathy"] = item["sympathy"]     # {ticker, relation, rationale}
    return json.dumps(payload, ensure_ascii=False)


def build_messages(item: dict, cluster: dict,
                   retry_error: str | None = None) -> list[dict]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for shot_in, shot_out in FEW_SHOT:
        messages.append({"role": "user", "content": json.dumps(shot_in, ensure_ascii=False)})
        messages.append({"role": "assistant", "content": json.dumps(shot_out, ensure_ascii=False)})
    user = render_item(item, cluster)
    if retry_error:
        user += ("\n\nYour previous response was invalid: " + retry_error +
                 "\nRespond again with ONLY a valid JSON object.")
    messages.append({"role": "user", "content": user})
    return messages

```

## `src/a1_triage/schema.py`

```python
"""A1's output contract (queue-contracts-spec §6 `triage` object).

One model, two enforcement points:
  * model-side: model_json_schema() is sent to llama-server as the grammar
    constraint, so off-contract output can't be generated;
  * code-side: validate_triage() re-checks anyway (spec §13 — models propose,
    code disposes; the stub backend and any future backend get no free pass).
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class TriageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    material: bool
    tickers: list[str] = Field(default_factory=list, max_length=8)
    direction_hint: Literal["up", "down", "unclear"] = "unclear"
    urgency: Literal["high", "medium", "low"] = "low"
    novelty_score: float = Field(ge=0.0, le=1.0, default=0.0)
    reason: str = Field(min_length=1, max_length=400)

    @field_validator("tickers")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        out = []
        for t in v:
            t = t.strip().upper()
            # plausible US equity symbol: 1-5 letters, optional .X class suffix
            if t and len(t) <= 7 and t.replace(".", "").isalpha():
                out.append(t)
        return list(dict.fromkeys(out))          # dedupe, keep order


def triage_json_schema() -> dict:
    """Schema for the server-side grammar constraint."""
    return TriageOutput.model_json_schema()


class TriageValidationError(Exception):
    def __init__(self, detail: str, raw: str):
        self.detail = detail[:500]
        self.raw = raw[:4000]
        super().__init__(detail)


def validate_triage(raw_text: str) -> TriageOutput:
    """Parse + validate model output. Raises TriageValidationError with a
    message suitable for appending to the retry prompt."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise TriageValidationError(f"output is not valid JSON: {e}", raw_text)
    try:
        return TriageOutput(**data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                         for x in e.errors()[:4])
        raise TriageValidationError(f"schema violations: {errs}", raw_text)

```

## `src/a1_triage/service.py`

```python
"""A1 Triage + Router service (Phase 2, observe-only).

Consumer loop per message on signal.triage (DedupedSignal, spec §5):
  1. run_triage() against the configured backend (grammar-constrained)
  2. compute routing facts (code)
  3. apply the four routing rules (pure function)
  4. ONE TRANSACTION: journal.decisions row + all routing enqueues
     — a decision can't exist without its routing, and vice versa
  5. material=true -> promote to the retrieval collection (outside the tx;
     idempotent upsert, safe under at-least-once redelivery)
  6. ack

TriageRejected -> REJECT decision row with the raw output in payload; ack
(the message is handled — the failure is journaled, not retried forever).
Infrastructure errors -> queue.fail() -> backoff -> DLQ (spec §1).

TriagedSignal enqueued downstream = spec §6 shape: item_ref + triage + routing.
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal

from common.config import config_path, load_yaml
from common.contracts import envelope
from common.db import close_pool, get_pool
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from common.queue import ack, claim, enqueue, fail, wait_for_message
from c1_ingestion.heartbeat import set_health
from c2_dedup.embedder import embed_text_for, get_embedder
from c2_dedup.vectorstore import VectorStore
from router.facts import compute_facts
from router.rules import route

from .backends import get_backend
from .triage import TriageRejected, run_triage

log = get_logger("a1.service")

IN_QUEUE = "signal.triage"
SYNTHETIC_QUEUE = "signal.synthetic"
CONSUMER = f"a1-{os.getpid()}"
CONTRACT_TRIAGED = "signal.triaged/1"


async def _fetch_item_and_cluster(item_id: str, revision: int) -> tuple[dict, dict] | None:
    """Parent item + its cluster corroboration, for synthetic re-entry."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT item_id, revision, is_correction, source, source_tier,
                      headline, summary, symbols, channels, published_ts, received_ts
               FROM news.news_items WHERE item_id = %s AND revision = %s""",
            (item_id, revision))
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
        item = dict(zip(cols, row))
        item["published_ts"] = item["published_ts"].isoformat()
        item["received_ts"] = item["received_ts"].isoformat()
        cur = await conn.execute(
            """SELECT c.cluster_id, c.independent_outlets, c.total_items
               FROM news.cluster_members cm
               JOIN news.cluster_corroboration c ON c.cluster_id = cm.cluster_id
               WHERE cm.item_id = %s LIMIT 1""", (item_id,))
        crow = await cur.fetchone()
        cluster = ({"cluster_id": crow[0], "is_new_story": False,
                    "independent_outlets": crow[1], "total_items": crow[2],
                    "similarity_to_canonical": 1.0} if crow else
                   {"cluster_id": None, "is_new_story": False,
                    "independent_outlets": 1, "total_items": 1,
                    "similarity_to_canonical": 1.0})
        return item, cluster


class A1Service:
    def __init__(self, cfg: dict, backend=None, store: VectorStore | None = None):
        self.cfg = cfg
        self.backend = backend or get_backend(cfg["model"])
        self.retries = int(cfg["model"].get("retries_on_invalid", 1))
        self.router_cfg = cfg["router"]
        self.store = store or VectorStore()
        self.embedder = get_embedder()

    async def handle(self, msg) -> None:
        body = msg.payload.get("body") or {}
        item = body.get("item") or {}
        cluster = body.get("cluster") or {}
        item_id = item.get("item_id")
        revision = int(item.get("revision") or 1)
        if not item_id or not item.get("headline"):
            raise ValueError(f"malformed DedupedSignal ({msg.dedup_key})")
        signal_id = item_id                       # news-origin signals: signal_id = item_id

        try:
            result = await run_triage(self.backend, item, cluster, self.retries)
        except TriageRejected as rej:
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                stage="TRIAGE", agent="A1", action="REJECT",
                payload={"raw_output": rej.raw, "error": rej.detail,
                         "attempts": rej.attempts},
                reason=f"model output invalid after {rej.attempts} attempts",
                model_id=rej.model_id, latency_ms=rej.latency_ms)
            log.warning("triage REJECT journaled", extra=kv(item_id=item_id))
            return

        triage = result.triage
        facts = await compute_facts(
            tickers=triage.tickers, source_tier=int(item.get("source_tier", 3)),
            urgency=triage.urgency, novelty=triage.novelty_score,
            independent_outlets=int(cluster.get("independent_outlets", 1)),
            router_cfg=self.router_cfg)
        decision = route(triage, facts,
                         overnight_base=int(self.router_cfg.get("overnight_base", 50)))

        triaged_body = {
            "item_ref": {"item_id": item_id, "revision": revision,
                         "cluster_id": cluster.get("cluster_id")},
            "triage": triage.model_dump(),
            "routing": facts.payload(),
        }

        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=signal_id, item_id=item_id, item_revision=revision,
                    ticker=triage.tickers[0] if triage.tickers else None,
                    stage="TRIAGE", agent="A1", action=decision.action,
                    payload={"triage": triage.model_dump(),
                             "routing": facts.payload(),
                             "routes": [r.queue for r in decision.routes]},
                    reason=triage.reason,
                    model_id=result.model_id, latency_ms=result.latency_ms,
                    conn=conn)
                msg_out = envelope(CONTRACT_TRIAGED, "A1", signal_id, item_id,
                                   revision, triaged_body)
                msg_out["envelope"]["trace"]["decision_id"] = decision_id
                for r in decision.routes:
                    await enqueue(r.queue, f"{item_id}:{revision}", msg_out,
                                  priority=r.priority, conn=conn)

        if triage.material:
            vector = self.embedder.embed(
                embed_text_for(item.get("headline", ""), item.get("summary")))
            self.store.promote_to_retrieval(
                item_id, revision, vector,
                payload={"headline": item.get("headline"),
                         "tickers": triage.tickers,
                         "published_ts": item.get("published_ts")})

        log.info("triaged", extra=kv(
            item_id=item_id, rev=revision, action=decision.action,
            material=triage.material, tickers=",".join(triage.tickers) or "-",
            routes=",".join(r.queue for r in decision.routes) or "-",
            latency_ms=result.latency_ms))


    async def handle_synthetic(self, msg) -> None:
        """Sympathy-lane re-entry (spec §10): triage the parent item FOR the
        sympathy ticker — same gates, no shortcuts. signal_id = synthetic_id;
        derived_from carries lineage; trace.ticker overrides A2's primary."""
        body = msg.payload.get("body") or {}
        syn_id = body.get("synthetic_id")
        parent = body.get("derived_from_item") or {}
        if not syn_id or not parent.get("item_id") or not body.get("ticker"):
            raise ValueError(f"malformed SyntheticSignal ({msg.dedup_key})")

        fetched = await _fetch_item_and_cluster(parent["item_id"],
                                                int(parent.get("revision") or 1))
        if fetched is None:
            raise ValueError(f"parent item not found: {parent}")
        item, cluster = fetched
        item = {**item, "sympathy": {"ticker": body["ticker"],
                                     "relation": body.get("relation"),
                                     "rationale": body.get("rationale")}}

        try:
            result = await run_triage(self.backend, item, cluster, self.retries)
        except TriageRejected as rej:
            await write_decision(
                signal_id=syn_id, item_id=parent["item_id"],
                item_revision=parent.get("revision"), ticker=body["ticker"],
                stage="TRIAGE", agent="A1", action="REJECT",
                payload={"raw_output": rej.raw, "error": rej.detail,
                         "synthetic": True},
                reason="synthetic triage output invalid",
                model_id=rej.model_id, latency_ms=rej.latency_ms,
                derived_from=body.get("derived_from_decision"))
            return

        triage = result.triage
        facts = await compute_facts(
            tickers=[body["ticker"]], source_tier=int(item.get("source_tier", 3)),
            urgency=triage.urgency, novelty=triage.novelty_score,
            independent_outlets=int(cluster.get("independent_outlets", 1)),
            router_cfg=self.router_cfg)
        decision = route(triage, facts,
                         overnight_base=int(self.router_cfg.get("overnight_base", 50)))

        triaged_body = {
            "item_ref": {"item_id": parent["item_id"],
                         "revision": parent.get("revision"),
                         "cluster_id": cluster.get("cluster_id")},
            "triage": triage.model_dump(),
            "routing": facts.payload(),
        }
        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=syn_id, item_id=parent["item_id"],
                    item_revision=parent.get("revision"), ticker=body["ticker"],
                    stage="TRIAGE", agent="A1", action=decision.action,
                    payload={"triage": triage.model_dump(),
                             "routing": facts.payload(), "synthetic": True,
                             "relation": body.get("relation")},
                    reason=triage.reason, model_id=result.model_id,
                    latency_ms=result.latency_ms,
                    derived_from=body.get("derived_from_decision"), conn=conn)
                out = envelope(CONTRACT_TRIAGED, "A1", syn_id,
                               parent["item_id"],
                               int(parent.get("revision") or 1), triaged_body)
                out["envelope"]["trace"]["decision_id"] = decision_id
                out["envelope"]["trace"]["ticker"] = body["ticker"]
                out["envelope"]["trace"]["derived_from_decision"] = \
                    body.get("derived_from_decision")
                for r in decision.routes:
                    await enqueue(r.queue, syn_id, out, priority=r.priority,
                                  conn=conn)
        log.info("synthetic triaged", extra=kv(
            synthetic_id=syn_id, ticker=body["ticker"],
            action=decision.action,
            routes=",".join(r.queue for r in decision.routes) or "-"))


async def consume_loop(svc: A1Service, stop: asyncio.Event) -> None:
    await set_health("triage", "OK", f"consuming {IN_QUEUE} + {SYNTHETIC_QUEUE}")
    while not stop.is_set():
        msg = await claim(IN_QUEUE, CONSUMER)
        handler = svc.handle
        if msg is None:
            msg = await claim(SYNTHETIC_QUEUE, CONSUMER)
            handler = svc.handle_synthetic
        if msg is None:
            try:
                await asyncio.wait_for(wait_for_message(IN_QUEUE, timeout_secs=5.0), 6.0)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await handler(msg)
            await ack(msg.msg_id)
        except Exception as e:
            log.error("message failed", extra=kv(msg_id=msg.msg_id, error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def main() -> None:
    cfg = load_yaml(config_path("a1.yaml"))
    await register_config_version("a1 triage service startup")
    svc = A1Service(cfg)
    log.info("A1 up", extra=kv(backend=cfg["model"].get("backend"),
                               model=svc.backend.model_id, consumer=CONSUMER))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await consume_loop(svc, stop)
    await set_health("triage", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

```

## `src/a1_triage/triage.py`

```python
"""A1 invocation core: model call -> code-side validation -> one retry with
the error appended -> TriageResult or TriageRejected. Never crashes on model
misbehavior; never drops (the REJECT lands in the journal with raw output).
"""
from __future__ import annotations

from dataclasses import dataclass

from common.log import get_logger, kv

from .backends import ModelBackend
from .prompt import build_messages
from .schema import TriageOutput, TriageValidationError, triage_json_schema, validate_triage

log = get_logger("a1.triage")


@dataclass
class TriageResult:
    triage: TriageOutput
    model_id: str
    latency_ms: int
    attempts: int


class TriageRejected(Exception):
    """Model failed to produce contract-valid output within the retry budget."""
    def __init__(self, detail: str, raw: str, model_id: str, latency_ms: int, attempts: int):
        self.detail = detail
        self.raw = raw
        self.model_id = model_id
        self.latency_ms = latency_ms
        self.attempts = attempts
        super().__init__(detail)


async def run_triage(backend: ModelBackend, item: dict, cluster: dict,
                     retries_on_invalid: int = 1) -> TriageResult:
    schema = triage_json_schema()
    total_latency = 0
    error: TriageValidationError | None = None

    for attempt in range(1 + retries_on_invalid):
        messages = build_messages(item, cluster,
                                  retry_error=error.detail if error else None)
        reply = await backend.complete(messages, schema)
        total_latency += reply.latency_ms
        try:
            triage = validate_triage(reply.text)
            return TriageResult(triage=triage, model_id=reply.model_id,
                                latency_ms=total_latency, attempts=attempt + 1)
        except TriageValidationError as e:
            error = e
            log.warning("invalid triage output",
                        extra=kv(attempt=attempt + 1, detail=e.detail[:120]))

    raise TriageRejected(detail=error.detail, raw=error.raw,
                         model_id=reply.model_id, latency_ms=total_latency,
                         attempts=1 + retries_on_invalid)

```

## `src/a2_analyst/__init__.py`

```python

```

## `src/a2_analyst/context.py`

```python
"""A2 context pack — everything the analyst sees beyond the item itself,
assembled by code (baseline: "answered against actual price action provided
in context", never model memory).

Included in Phase 3:
  price_action      pre-news reference, last, % move since received_ts,
                    volume multiple vs 20d average minute volume
  daily_context     prev close, ATR(14), ADV(20)
  related_headlines top-k from the retrieval collection (material items only)
  regime            latest C8 snapshot features
Deferred (P1 sources not yet integrated; keys present, value null, so the
prompt shape is stable): sector, earnings_date, short_interest, thesis_matches.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from common.clock import parse_ts, utcnow
from common.db import get_pool
from common.log import get_logger
from common.marketdata import MarketData, adv20, atr14, avg_minute_volume
from c2_dedup.embedder import embed_text_for
from c2_dedup.vectorstore import VectorStore

log = get_logger("a2.context")


async def _regime_features() -> tuple[int | None, dict | None]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT regime_id, features FROM journal.regime_snapshots
               ORDER BY ts DESC LIMIT 1""")
        row = await cur.fetchone()
        return (row[0], row[1]) if row else (None, None)


async def build_context(md: MarketData, store: VectorStore, embedder,
                        item: dict, ticker: str) -> tuple[dict, int | None]:
    """Returns (context dict for the prompt, regime_id for the decision row)."""
    received = parse_ts(item.get("received_ts") or item["published_ts"])
    now = utcnow()

    daily = await md.daily_bars(ticker, 30)
    quote = await md.snapshot(ticker)
    prev = await md.prev_close(ticker)

    # pre-news reference: last minute close before received_ts, else prev close
    pre_start = received - timedelta(minutes=30)
    pre_bars = await md.minute_bars(ticker, pre_start, received)
    prenews_price = pre_bars[-1]["close"] if pre_bars else prev

    since_bars = await md.minute_bars(ticker, received, now)
    baseline_bars = await md.minute_bars(ticker, received - timedelta(days=5), received)
    base_vol = avg_minute_volume(baseline_bars)
    since_vol = avg_minute_volume(since_bars)

    pct_move = round((quote.price - prenews_price) / prenews_price, 5) if prenews_price else None
    vol_mult = round(since_vol / base_vol, 2) if (since_vol and base_vol) else None

    related = []
    if item.get("headline"):
        vec = embedder.embed(embed_text_for(item["headline"], item.get("summary")))
        for hit in store.related(vec, limit=6):
            if hit.get("item_id") != item.get("item_id"):
                related.append({"headline": hit.get("headline"),
                                "tickers": hit.get("tickers"),
                                "published_ts": hit.get("published_ts"),
                                "similarity": round(hit.get("score", 0.0), 3)})

    regime_id, regime = await _regime_features()

    context = {
        "price_action": {
            "prenews_price": prenews_price,
            "last": quote.price,
            "pct_move_since_news": pct_move,
            "volume_multiple": vol_mult,
            "minutes_since_news": int((now - received).total_seconds() // 60),
        },
        "daily_context": {
            "prev_close": prev,
            "atr_14": atr14(daily),
            "adv_20d": adv20(daily),
        },
        "related_headlines": related[:5],
        "regime": regime,
        # P1 sources, deferred — stable keys, null values:
        "sector": None,
        "earnings_date": None,
        "short_interest": None,
        "thesis_matches": [],
    }
    return context, regime_id

```

## `src/a2_analyst/prompt.py`

```python
"""A2 analyst prompt. Doctrine:

The analyst turns an escalated item into a falsifiable thesis. It must answer
the mandatory question — "is this already priced in?" — against the ACTUAL
price action in context, not intuition. Invalidations are authored in two
buckets at write time: machine_checkable (compiled into C4 monitors — only
the closed DSL vocabulary is accepted) and news_checkable (A12's watch-list,
free text). Magnitude is a fraction (0.055 = 5.5%). Confidence is ordinal.
"""
from __future__ import annotations

import json

from common.invalidation_dsl import STDLIB

SYSTEM_PROMPT = f"""\
You are the analyst in a news-driven, LONG-ONLY US equities pipeline. You
receive one triaged news item plus code-computed market context. Produce a
falsifiable trade thesis as JSON.

Rules:
- MANDATORY: answer "is this already priced in?" using the price_action
  numbers provided (pct_move_since_news vs your magnitude_est). If the move
  since news already captures most of your estimate, say so in
  priced_in_assessment and lower confidence accordingly.
- magnitude_est is the FURTHER move you expect from here, as a fraction
  (0.03 = 3%). Be conservative; the confirmation gate punishes overclaiming.
- direction: the expected move of the stock. The system only enters longs;
  a "down" thesis is still valuable (it blocks entries and informs guards).
- expected_move_window: like "2_sessions" or "3_weeks" — when the move should
  complete. horizon: SHORT (days) or LONG (weeks+).
- source_risk: how much this thesis depends on the report being true.
  Tier-3 single-source rumor = "high". Tier-1 filing = "low".
- invalidation.machine_checkable: 0-2 entries from EXACTLY this vocabulary
  (price-observable conditions compiled into automated monitors):
  {sorted(STDLIB.keys())}
  Pick the ones that would falsify YOUR thesis. Do not invent names.
- invalidation.news_checkable: 0-3 short phrases describing news events that
  would kill the thesis (e.g. "counterparty denies talks").
- related_opportunities: up to 3 second-order names (suppliers, customers,
  competitors) ONLY when the causal link is direct and obvious. Empty is fine.
- reason: 2-4 sentences of plain reasoning.
- confidence: 0.0-1.0, ordinal only — it ranks your own theses, nothing more.

Respond with ONLY a JSON object matching the required schema."""


def build_messages(item: dict, triage: dict, context: dict,
                   retry_error: str | None = None) -> list[dict]:
    user_payload = {
        "item": {
            "headline": item.get("headline"),
            "summary": item.get("summary"),
            "source": item.get("source"),
            "source_tier": item.get("source_tier"),
            "channels": item.get("channels", []),
            "is_correction": item.get("is_correction", False),
            "published_ts": item.get("published_ts"),
        },
        "triage": triage,
        "context": context,
    }
    user = json.dumps(user_payload, ensure_ascii=False, default=str)
    if retry_error:
        user += ("\n\nYour previous response was invalid: " + retry_error +
                 "\nRespond again with ONLY a valid JSON object.")
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]

```

## `src/a2_analyst/schema.py`

```python
"""A2's output contract (queue-contracts-spec §7 `thesis` object), strict-typed
like TriageOutput.

The Phase 3 hook: every machine_checkable invalidation is compiled against the
MIP DSL AT AUTHORING TIME — an entry must be either a stdlib predicate name or
a full spec dict that passes invalidation_dsl.validate(). An unmonitorable
invalidation is a validation error back to the model on retry; it cannot
enter the journal.
"""
from __future__ import annotations

import json
import re
from typing import Literal, Union

from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator)

from common.invalidation_dsl import MIPError, STDLIB, validate as mip_validate

_WINDOW = re.compile(r"^\d{1,2}_(sessions?|weeks?)$")


class RelatedOpportunity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticker: str = Field(min_length=1, max_length=7)
    relation: str = Field(min_length=1, max_length=40)     # supplier|customer|competitor|...
    rationale: str = Field(min_length=1, max_length=300)

    @field_validator("ticker")
    @classmethod
    def _sym(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.replace(".", "").isalpha():
            raise ValueError(f"implausible ticker {v!r}")
        return v


class Invalidation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine_checkable: list[Union[str, dict]] = Field(default_factory=list, max_length=4)
    news_checkable: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("machine_checkable")
    @classmethod
    def _mip_valid(cls, v: list) -> list:
        for entry in v:
            if isinstance(entry, str):
                if entry not in STDLIB:
                    raise ValueError(
                        f"unknown stdlib predicate {entry!r}; known: {sorted(STDLIB)}")
            elif isinstance(entry, dict):
                try:
                    mip_validate(entry)
                except MIPError as e:
                    raise ValueError(f"MIP spec invalid ({e.code}): {e}") from e
            else:
                raise ValueError("entries must be stdlib names or MIP spec objects")
        return v


class ThesisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ticker: str = Field(min_length=1, max_length=7)
    direction: Literal["up", "down"]
    magnitude_est: float = Field(gt=0.0, le=0.5)           # fraction, e.g. 0.055 = 5.5%
    expected_move_window: str
    horizon: Literal["SHORT", "LONG"]
    confidence: float = Field(ge=0.0, le=1.0)              # ordinal (baseline rule 6)
    priced_in_assessment: str = Field(min_length=1, max_length=300)
    source_risk: Literal["low", "medium", "high"]
    invalidation: Invalidation
    related_opportunities: list[RelatedOpportunity] = Field(default_factory=list, max_length=3)
    reason: str = Field(min_length=1, max_length=600)

    @field_validator("ticker")
    @classmethod
    def _sym(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.replace(".", "").isalpha():
            raise ValueError(f"implausible ticker {v!r}")
        return v

    @field_validator("expected_move_window")
    @classmethod
    def _window(cls, v: str) -> str:
        if not _WINDOW.match(v):
            raise ValueError("expected_move_window must look like '2_sessions' or '3_weeks'")
        return v


def thesis_json_schema() -> dict:
    return ThesisOutput.model_json_schema()


class ThesisValidationError(Exception):
    def __init__(self, detail: str, raw: str):
        self.detail = detail[:600]
        self.raw = raw[:4000]
        super().__init__(detail)


def validate_thesis(raw_text: str) -> ThesisOutput:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ThesisValidationError(f"output is not valid JSON: {e}", raw_text)
    try:
        return ThesisOutput(**data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                         for x in e.errors()[:4])
        raise ThesisValidationError(f"schema violations: {errs}", raw_text)

```

## `src/a2_analyst/service.py`

```python
"""A2 Analyst service (Phase 3, observe-only).

Per message on signal.analyst (TriagedSignal, spec §6):
  1. fetch the item's latest revision from the news store (TriagedSignal
     carries item_ref only — the store is the source of truth)
  2. build the context pack (code): price action since news, related
     headlines from retrieval, regime features
  3. run the analyst model (grammar-constrained ThesisOutput; machine
     invalidations DSL-validated at parse time), one retry, else REJECT
  4. ONE TRANSACTION: ANALYST decision row + signal.gate enqueue (§7 shape)
     + one signal.synthetic enqueue per related opportunity (§10 shape)
  5. ack

Model slot: Analyst (Qwen3-32B Q5_K_M, llama-server :8081). Same retry/REJECT
discipline as A1. Down-direction theses still journal + gate (C3 handles the
long-only veto) — a bearish read is information, not an error.
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal

from common.config import config_path, load_yaml
from common.contracts import envelope
from common.db import close_pool, get_pool
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from common.queue import ack, claim, enqueue, fail, wait_for_message
from common.marketdata import get_marketdata
from c1_ingestion.heartbeat import set_health
from c2_dedup.embedder import get_embedder
from c2_dedup.vectorstore import VectorStore
from a1_triage.backends import get_backend
from a1_triage.triage import TriageRejected

from .context import build_context
from .prompt import build_messages
from .schema import ThesisValidationError, thesis_json_schema, validate_thesis

log = get_logger("a2.service")

IN_QUEUE = "signal.analyst"
GATE_QUEUE = "signal.gate"
SYNTHETIC_QUEUE = "signal.synthetic"
CONSUMER = f"a2-{os.getpid()}"
CONTRACT_THESIS = "signal.gate/1"
CONTRACT_SYNTHETIC = "signal.synthetic/1"


async def fetch_item(item_id: str, revision: int) -> dict | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT item_id, revision, is_correction, source, source_tier,
                      source_url, headline, summary, symbols, channels,
                      published_ts, received_ts
               FROM news.news_items WHERE item_id = %s AND revision = %s""",
            (item_id, revision))
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
        item = dict(zip(cols, row))
        item["published_ts"] = item["published_ts"].isoformat()
        item["received_ts"] = item["received_ts"].isoformat()
        return item


class A2Service:
    def __init__(self, cfg: dict, backend=None, md=None, store=None):
        self.cfg = cfg
        self.backend = backend or get_backend(cfg["model"])
        self.retries = int(cfg["model"].get("retries_on_invalid", 1))
        self.md = md or get_marketdata()
        self.store = store or VectorStore()
        self.embedder = get_embedder()

    async def handle(self, msg) -> None:
        body = msg.payload.get("body") or {}
        item_ref = body.get("item_ref") or {}
        triage = body.get("triage") or {}
        item_id = item_ref.get("item_id")
        revision = int(item_ref.get("revision") or 1)
        signal_id = (msg.payload.get("envelope", {}).get("trace", {})
                     .get("signal_id") or item_id)
        derived_from = (msg.payload.get("envelope", {}).get("trace", {})
                        .get("derived_from_decision"))
        if not item_id:
            raise ValueError(f"malformed TriagedSignal ({msg.dedup_key})")

        item = await fetch_item(item_id, revision)
        if item is None:
            raise ValueError(f"item not found in news store: {item_id} rev {revision}")

        # primary ticker: triage's first (synthetic signals override via trace)
        ticker = (msg.payload.get("envelope", {}).get("trace", {}).get("ticker")
                  or (triage.get("tickers") or [None])[0])
        if not ticker:
            raise ValueError(f"no ticker on analyst-lane signal {signal_id}")

        context, regime_id = await build_context(self.md, self.store,
                                                 self.embedder, item, ticker)

        schema = thesis_json_schema()
        error: ThesisValidationError | None = None
        total_latency = 0
        for attempt in range(1 + self.retries):
            messages = build_messages(item, triage, context,
                                      retry_error=error.detail if error else None)
            reply = await self.backend.complete(messages, schema)
            total_latency += reply.latency_ms
            try:
                thesis = validate_thesis(reply.text)
                break
            except ThesisValidationError as e:
                error = e
                log.warning("invalid thesis output",
                            extra=kv(attempt=attempt + 1, detail=e.detail[:150]))
        else:
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                ticker=ticker, stage="ANALYST", agent="A2", action="REJECT",
                payload={"raw_output": error.raw, "error": error.detail,
                         "attempts": 1 + self.retries},
                reason=f"model output invalid after {1 + self.retries} attempts",
                model_id=reply.model_id, latency_ms=total_latency,
                regime_id=regime_id, derived_from=derived_from)
            log.warning("thesis REJECT journaled", extra=kv(item_id=item_id))
            return

        gate_body = {"item_ref": item_ref,
                     "thesis": thesis.model_dump(),
                     "regime_id": regime_id}

        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=signal_id, item_id=item_id, item_revision=revision,
                    ticker=thesis.ticker, stage="ANALYST", agent="A2",
                    action="THESIS",
                    payload={"thesis": thesis.model_dump(), "context": context},
                    reason=thesis.reason, confidence=thesis.confidence,
                    model_id=self.backend.model_id, latency_ms=total_latency,
                    regime_id=regime_id, derived_from=derived_from, conn=conn)

                out = envelope(CONTRACT_THESIS, "A2", signal_id, item_id,
                               revision, gate_body)
                out["envelope"]["trace"]["decision_id"] = decision_id
                await enqueue(GATE_QUEUE, f"{signal_id}:{revision}", out, conn=conn)

                for opp in thesis.related_opportunities:
                    syn_id = f"syn-{decision_id}-{opp.ticker}"
                    syn = envelope(CONTRACT_SYNTHETIC, "A2", syn_id, item_id,
                                   revision, {
                                       "synthetic_id": syn_id,
                                       "derived_from_decision": decision_id,
                                       "derived_from_item": {"item_id": item_id,
                                                             "revision": revision},
                                       "ticker": opp.ticker,
                                       "relation": opp.relation,
                                       "rationale": opp.rationale,
                                   })
                    await enqueue(SYNTHETIC_QUEUE, syn_id, syn, conn=conn)

        log.info("thesis", extra=kv(
            signal_id=signal_id, ticker=thesis.ticker, dir=thesis.direction,
            mag=thesis.magnitude_est, conf=thesis.confidence,
            synthetics=len(thesis.related_opportunities),
            latency_ms=total_latency))


async def consume_loop(svc: A2Service, stop: asyncio.Event) -> None:
    await set_health("analyst", "OK", f"consuming {IN_QUEUE}")
    while not stop.is_set():
        msg = await claim(IN_QUEUE, CONSUMER)
        if msg is None:
            try:
                await asyncio.wait_for(wait_for_message(IN_QUEUE, timeout_secs=5.0), 6.0)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await svc.handle(msg)
            await ack(msg.msg_id)
        except Exception as e:
            log.error("message failed", extra=kv(msg_id=msg.msg_id, error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def main() -> None:
    cfg = load_yaml(config_path("a2.yaml"))
    await register_config_version("a2 analyst service startup")
    svc = A2Service(cfg)
    log.info("A2 up", extra=kv(backend=cfg["model"].get("backend"),
                               model=svc.backend.model_id, consumer=CONSUMER))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await consume_loop(svc, stop)
    await set_health("analyst", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

```

## `src/a3_risk/__init__.py`

```python

```

## `src/a3_risk/service.py`

```python
"""A3 Risk/PM service (Phase 4).

Consumes signal.risk (GatePass). Discretion first (bounded LLM adjustment of
k / realization_fraction / time_window within config bands — invalid or
failed output falls back to profile defaults, journaled; the trade never
blocks on the model). Then the deterministic sizing chain. Then ONE
TRANSACTION: RISK decision + intents row + exec.intent enqueue.

intent_id = sha256(signal_id:revision:config_version)[:24] — crash-replay of
the same gated signal can never double-submit, even across config changes
(a new config version is deliberately a new intent).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal as _signal
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.clock import utcnow
from common.config import config_path, load_yaml
from common.contracts import envelope
from common.db import get_pool, close_pool
from common.journal import (active_config_version, register_config_version,
                            write_decision)
from common.log import get_logger, kv
from common.queue import ack, claim, enqueue, fail, wait_for_message
from c1_ingestion.heartbeat import set_health
from a1_triage.backends import get_backend
from router.facts import _schedule_cache

from .sizing import (SizingInputs, hard_gates, open_risk_dollars,
                     size_entry)

log = get_logger("a3.service")

IN_QUEUE = "signal.risk"
OUT_QUEUE = "exec.intent"
CONSUMER = f"a3-{os.getpid()}"
CONTRACT_INTENT = "exec.intent/1"


# ---------------------------------------------------------------------------
# Bounded discretion
# ---------------------------------------------------------------------------

class RiskAdjustments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    k: float
    realization_fraction: float
    time_window_sessions: int
    reason: str = Field(min_length=1, max_length=300)


def validate_adjustments(raw: str, bands: dict) -> RiskAdjustments:
    data = json.loads(raw)
    adj = RiskAdjustments(**data)
    lo, hi = bands["k"]
    if not lo <= adj.k <= hi:
        raise ValueError(f"k {adj.k} outside band [{lo},{hi}]")
    lo, hi = bands["realization_fraction"]
    if not lo <= adj.realization_fraction <= hi:
        raise ValueError(f"realization_fraction {adj.realization_fraction} outside band")
    lo, hi = bands["time_window_sessions"]
    if not lo <= adj.time_window_sessions <= hi:
        raise ValueError(f"time_window_sessions {adj.time_window_sessions} outside band")
    return adj


def adjustments_schema() -> dict:
    return RiskAdjustments.model_json_schema()


DISCRETION_PROMPT = """\
You are the risk sizing adjuster in a long-only news pipeline. Given the
thesis and gate confirmation numbers, choose within the allowed bands:
- k: stop width multiplier on ATR(14). Wider (higher k) for volatile/gappy
  setups or lower confidence; tighter for clean high-confidence confirmations.
- realization_fraction: fraction of the predicted move at which to scale out.
- time_window_sessions: sessions to allow before the time stop.
Bands: k {k_band}, realization_fraction {rf_band}, time_window_sessions {tw_band}.
Defaults if unsure: k={k_default}, realization_fraction={rf_default},
time_window={tw_default}. Respond ONLY with JSON: {{"k": .., 
"realization_fraction": .., "time_window_sessions": .., "reason": ".."}}"""


# ---------------------------------------------------------------------------
# Context gathering (all reads; A3 never calls the broker)
# ---------------------------------------------------------------------------

async def read_controls() -> dict:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT key, value FROM journal.control")
        rows = await cur.fetchall()
    return {k: v for k, v in rows}


async def portfolio_state() -> tuple[dict, float]:
    """(open heat per lane from CURRENT stops, deployed notional)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT horizon, qty_open, avg_entry,
                      (exit_policy->'initial_stop'->>'price')::numeric
               FROM journal.positions WHERE status='OPEN'""")
        rows = await cur.fetchall()
    heat = {"SHORT": 0.0, "LONG": 0.0}
    notional = 0.0
    for horizon, qty_open, avg_entry, stop in rows:
        heat[horizon] += open_risk_dollars(qty_open, float(avg_entry),
                                           float(stop or 0))
        notional += qty_open * float(avg_entry)
    return heat, notional


async def trades_today() -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT count(*) FROM journal.intents
               WHERE side='BUY' AND ts::date = (now() AT TIME ZONE 'UTC')::date
                 AND status NOT IN ('REJECTED')""")
        return (await cur.fetchone())[0]


async def ticker_halted(ticker: str) -> bool:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT count(*) FROM journal.position_events pe
               JOIN journal.positions p USING (position_id)
               WHERE p.ticker=%s AND pe.event_type='HALT_FROZEN'
                 AND NOT EXISTS (SELECT 1 FROM journal.position_events r
                                 WHERE r.position_id=pe.position_id
                                   AND r.event_type='HALT_RESUMED'
                                   AND r.ts > pe.ts)""", (ticker,))
        return (await cur.fetchone())[0] > 0


def minutes_to_close(now: datetime) -> Optional[int]:
    import pandas_market_calendars as mcal
    day_key = now.strftime("%Y-%m-%d")
    if day_key not in _schedule_cache:
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=day_key, end_date=day_key)
        _schedule_cache[day_key] = None if sched.empty else (
            sched.iloc[0]["market_open"].to_pydatetime(),
            sched.iloc[0]["market_close"].to_pydatetime())
    win = _schedule_cache[day_key]
    if win is None or not (win[0] <= now < win[1]):
        return None
    return int((win[1] - now).total_seconds() // 60)


async def earnings_next_sessions(ticker: str) -> Optional[int]:
    """D7 deferred: no earnings-calendar source yet. Always None (unknown) —
    A3 journals the EARNINGS_UNKNOWN flag. Replace when the P1 source lands."""
    return None


async def thesis_decision_id(signal_id: str) -> Optional[int]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT decision_id FROM journal.decisions
               WHERE signal_id=%s AND stage='ANALYST' AND action='THESIS'
               ORDER BY ts DESC LIMIT 1""", (signal_id,))
        row = await cur.fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class A3Service:
    def __init__(self, cfg: dict, profiles: dict, backend=None, now_fn=None):
        self.capital = cfg["capital"]
        self.limits = cfg["limits"]
        self.profiles = profiles["profiles"]
        self.bands = profiles["discretion_bands"]
        self.backend = backend or get_backend(cfg["model"])
        self.now_fn = now_fn or utcnow

    def profile_for(self, horizon: str) -> tuple[str, dict]:
        name = "short_term_v1" if horizon == "SHORT" else "long_term_v1"
        return name, self.profiles[name]

    async def discretion(self, thesis: dict, gate: dict,
                         profile: dict) -> tuple[RiskAdjustments, bool]:
        """Returns (adjustments, model_used). Any failure -> profile defaults."""
        defaults = RiskAdjustments(
            k=float(profile["initial_stop"]["k"]),
            realization_fraction=float(profile["realization"]["target_fraction"]),
            time_window_sessions=int(thesis["expected_move_window"].split("_")[0])
            if thesis["expected_move_window"].endswith("sessions") else 2,
            reason="profile defaults")
        prompt = DISCRETION_PROMPT.format(
            k_band=self.bands["k"], rf_band=self.bands["realization_fraction"],
            tw_band=self.bands["time_window_sessions"],
            k_default=defaults.k, rf_default=defaults.realization_fraction,
            tw_default=defaults.time_window_sessions)
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(
                        {"thesis": thesis, "gate_numbers": {
                            k_: gate.get(k_) for k_ in
                            ("pct_move", "vol_mult", "minutes", "rule")}})}]
        try:
            reply = await self.backend.complete(messages, adjustments_schema())
            return validate_adjustments(reply.text, self.bands), True
        except Exception as e:
            log.warning("discretion fallback to defaults",
                        extra=kv(error=repr(e)[:150]))
            defaults.reason = f"fallback: {repr(e)[:120]}"
            return defaults, False

    def materialize_exit_policy(self, profile_name: str, profile: dict,
                                adj: RiskAdjustments, limit_price: float,
                                atr: float, thesis: dict) -> dict:
        stop_price = round(limit_price - adj.k * atr, 2)
        cat_price = round(limit_price - profile["catastrophe"]["k"] * atr, 2)
        return {
            "profile": profile_name,
            "initial_stop": {"method": "atr", "k": adj.k, "price": stop_price},
            "catastrophe_stop_broker": {"k": profile["catastrophe"]["k"],
                                        "price": cat_price},
            "breakeven_at_R": profile["breakeven_at_R"],
            "trail": dict(profile["trail"]),
            "time_stop": ({"window": f"{adj.time_window_sessions}_sessions",
                           "min_progress_R": profile["time_stop"]["min_progress_R"]}
                          if profile.get("time_stop") else None),
            "realization": {"target_fraction": adj.realization_fraction,
                            "action": profile["realization"]["action"]},
            "machine_invalidations": thesis["invalidation"]["machine_checkable"],
            "news_invalidations": thesis["invalidation"]["news_checkable"],
            "earnings_blackout_exit": profile["earnings_blackout_exit"],
            "overnight_hold": profile["overnight_hold"],
            "magnitude_est": thesis["magnitude_est"],
            "atr_14": atr,
        }

    async def handle(self, msg) -> None:
        body = msg.payload.get("body") or {}
        thesis = body.get("thesis") or {}
        gate = body.get("gate") or {}
        snapshot = gate.get("snapshot") or {}
        trace = msg.payload.get("envelope", {}).get("trace", {})
        signal_id = trace.get("signal_id")
        item_id = trace.get("item_id")
        revision = int(trace.get("revision") or 1)
        if not signal_id or not thesis.get("ticker") or not snapshot:
            raise ValueError(f"malformed GatePass ({msg.dedup_key})")
        ticker = thesis["ticker"]
        horizon = thesis["horizon"]
        profile_name, profile = self.profile_for(horizon)

        controls = await read_controls()
        heat, deployed = await portfolio_state()

        effective_capital = min(
            float(controls.get("broker_equity", "0") or 0),
            float(controls.get("trading_capital", "0") or 0))
        inp = SizingInputs(
            effective_capital=effective_capital,
            settled_cash=float(controls.get("settled_cash", "0") or 0),
            ref_price=float(snapshot["ref_price"]), bid=float(snapshot["bid"]),
            ask=float(snapshot["ask"]),
            spread_bps=float(snapshot["spread_bps"]),
            atr_14=snapshot.get("atr_14") and float(snapshot["atr_14"]),
            adv_20d=snapshot.get("adv_20d") and float(snapshot["adv_20d"]),
            open_heat=heat, deployed_notional=deployed,
            trades_today=await trades_today(),
            ticker_halted=await ticker_halted(ticker),
            kill_switch=controls.get("kill_switch") == "1",
            breaker=controls.get("drawdown_breaker") == "1",
            block_entries=controls.get("block_entries") == "1",
            max_trades_per_day=int(controls.get(
                "max_trades_per_day",
                str(self.limits["max_trades_per_day_default"]))),
            minutes_to_close=minutes_to_close(self.now_fn()),
            earnings_next_sessions=await earnings_next_sessions(ticker),
        )

        # hard gates BEFORE the model call: no tokens burned under a kill
        # switch, and operational vetoes dominate in the journal
        gate_veto, _, _ = hard_gates(inp, self.limits, profile)
        if gate_veto is not None:
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                ticker=ticker, stage="RISK", agent="A3", action="VETO",
                veto_reason=gate_veto.veto_reason,
                payload={"sizing": gate_veto.numbers, "flags": gate_veto.flags,
                         "effective_capital": effective_capital},
                reason=gate_veto.veto_reason,
                regime_id=body.get("regime_id"))
            log.info("risk VETO", extra=kv(signal_id=signal_id,
                                           reason=gate_veto.veto_reason))
            return

        # thesis lineage is load-bearing (positions.thesis_decision_id NOT
        # NULL): a GatePass whose ANALYST decision can't be found must not size
        tdid = await thesis_decision_id(signal_id)
        if tdid is None:
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                ticker=ticker, stage="RISK", agent="A3", action="VETO",
                veto_reason="NO_THESIS_LINEAGE",
                payload={"detail": "no ANALYST THESIS decision for signal"},
                reason="missing thesis lineage",
                regime_id=body.get("regime_id"))
            log.warning("risk VETO no thesis lineage",
                        extra=kv(signal_id=signal_id))
            return

        adj, model_used = await self.discretion(thesis, gate, profile)
        result = size_entry(inp, self.capital, self.limits, profile,
                            horizon, adj.k)

        payload = {"sizing": result.numbers, "flags": result.flags,
                   "adjustments": adj.model_dump(),
                   "model_used": model_used,
                   "effective_capital": effective_capital}

        if result.verdict == "VETO":
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                ticker=ticker, stage="RISK", agent="A3", action="VETO",
                veto_reason=result.veto_reason, payload=payload,
                reason=f"{result.veto_reason}",
                model_id=self.backend.model_id if model_used else None,
                regime_id=body.get("regime_id"))
            log.info("risk VETO", extra=kv(signal_id=signal_id,
                                           reason=result.veto_reason))
            return

        config_version = active_config_version()
        intent_id = hashlib.sha256(
            f"{signal_id}:{revision}:{config_version}".encode()).hexdigest()[:24]
        exit_policy = self.materialize_exit_policy(
            profile_name, profile, adj, result.limit_price,
            float(snapshot["atr_14"]), thesis)

        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=signal_id, item_id=item_id, item_revision=revision,
                    ticker=ticker, stage="RISK", agent="A3", action="SIZE",
                    payload={**payload, "intent_id": intent_id,
                             "exit_policy": exit_policy,
                             "thesis_decision_id": tdid},
                    reason=adj.reason,
                    model_id=self.backend.model_id if model_used else None,
                    regime_id=body.get("regime_id"), conn=conn)
                await conn.execute(
                    """INSERT INTO journal.intents
                       (intent_id, decision_id, ticker, side, qty, limit_price,
                        gate_snapshot, exit_policy, horizon, effective_capital,
                        risk_budget, status, config_version)
                       VALUES (%s,%s,%s,'BUY',%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
                       ON CONFLICT (intent_id) DO NOTHING""",
                    (intent_id, decision_id, ticker, result.qty,
                     result.limit_price, json.dumps(snapshot),
                     json.dumps(exit_policy), horizon, effective_capital,
                     result.risk_budget, config_version))
                out = envelope(CONTRACT_INTENT, "A3", signal_id, item_id,
                               revision, {
                                   "intent_id": intent_id,
                                   "ticker": ticker, "side": "BUY",
                                   "qty": result.qty,
                                   "limit_price": result.limit_price,
                                   "exit_policy": exit_policy,
                                   "gate_snapshot": snapshot,
                                   "horizon": horizon,
                                   "thesis_decision_id": tdid,
                                   "effective_capital": effective_capital,
                                   "risk_budget": result.risk_budget})
                out["envelope"]["trace"]["decision_id"] = decision_id
                await enqueue(OUT_QUEUE, intent_id, out, conn=conn)

        log.info("intent", extra=kv(signal_id=signal_id, ticker=ticker,
                                    qty=result.qty, limit=result.limit_price,
                                    risk=result.actual_risk,
                                    intent_id=intent_id))


async def consume_loop(svc: A3Service, stop: asyncio.Event) -> None:
    await set_health("risk", "OK", f"consuming {IN_QUEUE}")
    while not stop.is_set():
        msg = await claim(IN_QUEUE, CONSUMER)
        if msg is None:
            try:
                await asyncio.wait_for(wait_for_message(IN_QUEUE, timeout_secs=5.0), 6.0)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await svc.handle(msg)
            await ack(msg.msg_id)
        except Exception as e:
            log.error("message failed", extra=kv(msg_id=msg.msg_id, error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def main() -> None:
    cfg = load_yaml(config_path("risk.yaml"))
    profiles = load_yaml(config_path("exit_profiles.yaml"))
    await register_config_version("a3 risk service startup")
    svc = A3Service(cfg, profiles)
    log.info("A3 up", extra=kv(consumer=CONSUMER))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await consume_loop(svc, stop)
    await set_health("risk", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

```

## `src/a3_risk/sizing.py`

```python
"""A3 sizing chain (phase4-design-v1_0 §2) — pure functions, no I/O.

Models propose, code disposes: the LLM's only inputs here are k /
realization_fraction / time_window (already band-validated); every dollar
figure below is deterministic code. Allocation is a consequence of risk:
size = risk_budget / stop_distance, then clipped, then viability-checked.

Veto reasons (all journaled with numbers): KILL_SWITCH, BREAKER,
BLOCK_ENTRIES, HALTED, MAX_TRADES, ENTRY_BLACKOUT, WIDE_SPREAD,
EARNINGS_BLACKOUT, NO_ATR, SIZE_CLIPPED.
Flags (journaled, non-blocking): EARNINGS_UNKNOWN, SECTOR_UNKNOWN (D7).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class SizingInputs:
    # capital (from C4's reconciliation rows — A3 never calls the broker)
    effective_capital: float           # min(broker_equity, trading_capital)
    settled_cash: float
    # gate snapshot (C3)
    ref_price: float                   # snapshot ask-side reference
    bid: float
    ask: float
    spread_bps: float
    atr_14: Optional[float]
    adv_20d: Optional[float]
    # portfolio state
    open_heat: dict                    # {"SHORT": $risk, "LONG": $risk}
    deployed_notional: float
    trades_today: int
    ticker_halted: bool = False
    # operational controls / flags
    kill_switch: bool = False
    breaker: bool = False
    block_entries: bool = False
    max_trades_per_day: int = 5
    # session
    minutes_to_close: Optional[int] = None    # None off-hours (entries blocked upstream)
    # deferred-nullable context (D7)
    earnings_next_sessions: Optional[int] = None   # sessions until earnings; None unknown
    sector: Optional[str] = None
    sector_heat: Optional[float] = None


@dataclass
class SizingResult:
    verdict: str                       # SIZE | VETO
    veto_reason: Optional[str] = None
    qty: int = 0
    limit_price: float = 0.0
    stop_distance: float = 0.0
    initial_stop: float = 0.0
    catastrophe_stop: float = 0.0
    risk_budget: float = 0.0           # intended $ risk
    actual_risk: float = 0.0           # qty * stop_distance after clips
    numbers: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


def limit_price_from_snapshot(ask: float, spread_bps: float) -> float:
    """Snapshot ask + min(half-spread, 10bps) buffer — priced off the C3
    snapshot per baseline; buffer bounds chase without paying full spread."""
    buffer = min((spread_bps / 2) / 10_000, 0.0010) * ask
    return round(ask + buffer, 2)


def hard_gates(inp: SizingInputs, limits_cfg: dict, profile: dict,
               earnings_blackout_sessions: int = 1
               ) -> tuple[Optional[SizingResult], dict, list[str]]:
    """Absolute vetoes, cheapest first — separable so A3 can run them BEFORE
    the discretion model call (no LLM tokens burned under a kill switch).
    Returns (veto_result | None, numbers_so_far, flags_so_far)."""
    n: dict = {}
    flags: list[str] = []
    if inp.kill_switch:
        return SizingResult("VETO", "KILL_SWITCH", numbers=n), n, flags
    if inp.breaker:
        return SizingResult("VETO", "BREAKER", numbers=n), n, flags
    if inp.block_entries:
        return SizingResult("VETO", "BLOCK_ENTRIES", numbers=n), n, flags
    if inp.ticker_halted:
        return SizingResult("VETO", "HALTED", numbers=n), n, flags
    n["trades_today"] = inp.trades_today
    if inp.trades_today >= inp.max_trades_per_day:
        n["max_trades_per_day"] = inp.max_trades_per_day
        return SizingResult("VETO", "MAX_TRADES", numbers=n), n, flags
    if inp.minutes_to_close is not None and \
            inp.minutes_to_close <= limits_cfg["entry_blackout_final_min"]:
        n["minutes_to_close"] = inp.minutes_to_close
        return SizingResult("VETO", "ENTRY_BLACKOUT", numbers=n), n, flags
    n["spread_bps"] = inp.spread_bps
    if inp.spread_bps > limits_cfg["spread_max_bps"]:
        return SizingResult("VETO", "WIDE_SPREAD", numbers=n), n, flags
    if inp.earnings_next_sessions is None:
        flags.append("EARNINGS_UNKNOWN")           # D7: allow + flag during paper
    elif profile.get("earnings_blackout_exit") and \
            inp.earnings_next_sessions <= earnings_blackout_sessions:
        n["earnings_next_sessions"] = inp.earnings_next_sessions
        return SizingResult("VETO", "EARNINGS_BLACKOUT", numbers=n,
                            flags=flags), n, flags
    if inp.atr_14 is None or inp.atr_14 <= 0:
        return SizingResult("VETO", "NO_ATR", numbers=n, flags=flags), n, flags
    return None, n, flags


def size_entry(inp: SizingInputs, capital_cfg: dict, limits_cfg: dict,
               profile: dict, horizon: str, k_adj: float,
               earnings_blackout_sessions: int = 1) -> SizingResult:
    veto, n, flags = hard_gates(inp, limits_cfg, profile,
                                earnings_blackout_sessions)
    if veto is not None:
        return veto

    # ---- the chain -------------------------------------------------------------
    risk_budget = capital_cfg["risk_per_trade_pct"] * inp.effective_capital
    stop_distance = k_adj * inp.atr_14
    limit_price = limit_price_from_snapshot(inp.ask, inp.spread_bps)
    raw_qty = risk_budget / stop_distance
    n.update(risk_budget=round(risk_budget, 2),
             stop_distance=round(stop_distance, 4),
             limit_price=limit_price, k_adj=k_adj, raw_qty=round(raw_qty, 2))

    clips: dict[str, float] = {}
    # notional cap
    clips["notional"] = (capital_cfg["max_position_notional_pct"]
                         * inp.effective_capital) / limit_price
    # liquidity cap
    if inp.adv_20d:
        clips["adv"] = limits_cfg["adv_participation_max"] * inp.adv_20d
    # settled buying power (cash account)
    clips["settled_cash"] = max(inp.settled_cash, 0.0) / limit_price
    # deployed-notional pre-flight headroom
    clips["capital_headroom"] = max(
        inp.effective_capital - inp.deployed_notional, 0.0) / limit_price
    # portfolio heat, per-lane split
    lane_cap = capital_cfg["heat_split"][horizon] * inp.effective_capital
    lane_used = inp.open_heat.get(horizon, 0.0)
    clips["lane_heat"] = max(lane_cap - lane_used, 0.0) / stop_distance
    total_cap = capital_cfg["max_portfolio_heat_pct"] * inp.effective_capital
    total_used = sum(inp.open_heat.values())
    clips["total_heat"] = max(total_cap - total_used, 0.0) / stop_distance
    # sector heat (deferred-nullable, D7)
    if inp.sector is None:
        flags.append("SECTOR_UNKNOWN")
    elif inp.sector_heat is not None:
        sector_cap = capital_cfg["max_sector_heat_pct"] * inp.effective_capital
        clips["sector_heat"] = max(sector_cap - inp.sector_heat, 0.0) / stop_distance

    qty = math.floor(min(raw_qty, *clips.values()))
    binding = min(clips, key=lambda k_: clips[k_])
    n["clips"] = {k_: round(v, 2) for k_, v in clips.items()}
    n["binding_clip"] = binding if clips[binding] < raw_qty else None
    n["qty"] = qty

    actual_risk = qty * stop_distance
    n["actual_risk"] = round(actual_risk, 2)
    if qty <= 0 or actual_risk < capital_cfg["min_viable_risk_fraction"] * risk_budget:
        n["min_viable_risk_fraction"] = capital_cfg["min_viable_risk_fraction"]
        return SizingResult("VETO", "SIZE_CLIPPED", numbers=n, flags=flags)

    initial_stop = round(limit_price - stop_distance, 2)
    cat_k = profile["catastrophe"]["k"]
    catastrophe_stop = round(limit_price - cat_k * inp.atr_14, 2)
    n["catastrophe_k"] = cat_k

    return SizingResult("SIZE", None, qty=qty, limit_price=limit_price,
                        stop_distance=round(stop_distance, 4),
                        initial_stop=initial_stop,
                        catastrophe_stop=catastrophe_stop,
                        risk_budget=round(risk_budget, 2),
                        actual_risk=round(actual_risk, 2),
                        numbers=n, flags=flags)


def open_risk_dollars(qty_open: int, avg_entry: float, current_stop: float) -> float:
    """A position's contribution to portfolio heat: current stop distance x
    open shares (stop at/above entry contributes zero — house money)."""
    return max(avg_entry - current_stop, 0.0) * qty_open

```

## `src/c1_ingestion/__init__.py`

```python

```

## `src/c1_ingestion/heartbeat.py`

```python
"""C1 heartbeats and ingestion-gap tracking.

Two outputs:
  journal.health   — component status the dashboard and dead-man logic read
  news.ingestion_gaps — explicit "no data 2:14–5:30" rows surfaced to A4/A8

Gap semantics: per source, a Monitor tracks last_item_ts. When silence exceeds
the market-hours-aware threshold, one gap row opens (gap_end NULL while
ongoing); on the next item it closes. Threshold selection uses coarse RTH from
common.clock — a false-positive gap on a holiday is tolerable in Phase 1.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from common.clock import is_market_hours, utcnow
from common.db import get_pool
from common.log import get_logger, kv

log = get_logger("c1.heartbeat")


async def set_health(component: str, status: str, detail: str = "") -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO journal.health (component, status, detail, updated_ts)
               VALUES (%s,%s,%s, now())
               ON CONFLICT (component) DO UPDATE
               SET status = EXCLUDED.status, detail = EXCLUDED.detail,
                   updated_ts = EXCLUDED.updated_ts""",
            (component, status, detail[:500]),
        )


class GapMonitor:
    def __init__(self, source: str, market_threshold_secs: int, offhours_threshold_secs: int):
        self.source = source
        self.market_threshold = market_threshold_secs
        self.offhours_threshold = offhours_threshold_secs
        self.last_item_ts: datetime = utcnow()   # start of monitoring counts as activity
        self.open_gap_id: Optional[int] = None

    def _threshold(self) -> int:
        return self.market_threshold if is_market_hours() else self.offhours_threshold

    def mark_activity(self) -> None:
        self.last_item_ts = utcnow()

    async def check(self) -> None:
        """Called periodically by the watchdog. Opens/closes gap rows."""
        now = utcnow()
        silent = (now - self.last_item_ts).total_seconds()
        pool = await get_pool()

        if self.open_gap_id is None and silent > self._threshold():
            async with pool.connection() as conn:
                cur = await conn.execute(
                    """INSERT INTO news.ingestion_gaps (source, gap_start, detail)
                       VALUES (%s,%s,%s) RETURNING gap_id""",
                    (self.source, self.last_item_ts,
                     f"silent {int(silent)}s (threshold {self._threshold()}s)"),
                )
                self.open_gap_id = (await cur.fetchone())[0]
            log.warning("gap opened", extra=kv(source=self.source, silent_secs=int(silent)))
            await set_health(f"ingestion:{self.source}", "DEGRADED",
                             f"no items for {int(silent)}s")
        elif self.open_gap_id is not None and silent <= self._threshold():
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE news.ingestion_gaps SET gap_end = %s WHERE gap_id = %s",
                    (self.last_item_ts, self.open_gap_id),
                )
            log.info("gap closed", extra=kv(source=self.source, gap_id=self.open_gap_id))
            self.open_gap_id = None
            await set_health(f"ingestion:{self.source}", "OK", "recovered")

```

## `src/c1_ingestion/normalize.py`

```python
"""C1 normalization: raw source payloads -> validated NewsItem, or a
QuarantineItem with a reason code (v0.4: quarantine, never drop).

One function per source family. Each returns NewsItem on success and raises
NormalizeError(reason_code, detail) on failure; the caller quarantines.
The item is the immutable record of what the feed said — symbols come only
from feed tags, never inference (A1's job, Phase 2).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import ValidationError

from common.clock import parse_ts, utcnow
from common.contracts import NewsItem, content_hash


class NormalizeError(Exception):
    def __init__(self, reason_code: str, detail: str, raw=None, raw_text: str | None = None):
        self.reason_code = reason_code
        self.detail = detail[:500]
        self.raw = raw if isinstance(raw, dict) else None
        self.raw_text = raw_text or (None if isinstance(raw, dict) else repr(raw)[:2000])
        super().__init__(f"{reason_code}: {detail}")


MAX_RAW_BYTES = 512 * 1024  # OVERSIZE guard


def _require(payload: dict, key: str):
    if key not in payload or payload[key] in (None, ""):
        raise NormalizeError("MISSING_REQUIRED_FIELD", f"missing {key}", raw=payload)
    return payload[key]


def _ts_or_quarantine(payload: dict, value, field: str):
    try:
        return parse_ts(value)
    except ValueError as e:
        raise NormalizeError("BAD_TIMESTAMP", f"{field}: {e}", raw=payload)


def _build(payload: dict, **kwargs) -> NewsItem:
    try:
        return NewsItem(**kwargs)
    except ValidationError as e:
        raise NormalizeError("UNKNOWN_SCHEMA", f"contract validation: {e.errors()[:3]}", raw=payload)


# ---------------------------------------------------------------------------
# Alpaca news websocket (v1beta1/news). Message shape:
# {"T":"n","id":40892639,"headline":"...","summary":"...","author":"...",
#  "created_at":"...","updated_at":"...","symbols":["AAPL"],"url":"...",
#  "content":"...","source":"benzinga"}
# ---------------------------------------------------------------------------

def normalize_alpaca(payload: dict, tier: int = 2) -> NewsItem:
    if not isinstance(payload, dict):
        raise NormalizeError("UNPARSEABLE_JSON", "non-object message", raw_text=repr(payload)[:2000])
    if len(str(payload)) > MAX_RAW_BYTES:
        raise NormalizeError("OVERSIZE", f"payload > {MAX_RAW_BYTES}B", raw_text=str(payload)[:2000])

    alpaca_id = _require(payload, "id")
    headline = str(_require(payload, "headline")).strip()
    if not headline:
        raise NormalizeError("MISSING_REQUIRED_FIELD", "empty headline", raw=payload)

    created = _ts_or_quarantine(payload, _require(payload, "created_at"), "created_at")
    summary = (payload.get("summary") or "").strip() or None
    body = (payload.get("content") or "").strip() or None
    symbols = payload.get("symbols") or []
    if not isinstance(symbols, list):
        raise NormalizeError("UNKNOWN_SCHEMA", f"symbols not a list: {type(symbols)}", raw=payload)

    return _build(
        payload,
        item_id=f"alpaca:{alpaca_id}",
        source="alpaca_benzinga",
        source_tier=tier,
        source_url=payload.get("url") or None,
        author=payload.get("author") or None,
        headline=headline,
        summary=summary,
        content_hash=content_hash(headline, summary, body),
        raw=payload,
        symbols=[str(s) for s in symbols],
        channels=[],
        published_ts=created,
        received_ts=utcnow(),
    )


# ---------------------------------------------------------------------------
# SEC EDGAR current-events Atom entries (parsed by feedparser upstream).
# entry: {id, title, link, updated, summary, ...}; title like
# "8-K - ACME CORP (0001234567) (Filer)"
# ---------------------------------------------------------------------------

# Title shape: "FORM - Entity Name (0001234567) (Role)". FORM may contain
# spaces ("SCHEDULE 13G/A") — the old single-token pattern failed those
# titles entirely, dropping their form channel (observed as the bare
# {filing} bucket, 5.5k items/day). Non-greedy up to the first " - ".
_EDGAR_TITLE = re.compile(
    r"^\s*(.+?)\s+-\s+(.*?)\s*(?:\((\d{10})\))?\s*(?:\(([^)]*)\))?\s*$")
_ACCESSION = re.compile(r"accession[-_]?number=([\d\-]+)", re.IGNORECASE)


def edgar_accession(entry: dict) -> str | None:
    """Accession number from an EDGAR Atom entry (id or link), or None."""
    m = _ACCESSION.search(str(entry.get("id") or "")) or \
        _ACCESSION.search(str(entry.get("link") or ""))
    return m.group(1) if m else None


def edgar_title_parts(title: str) -> tuple[str | None, str | None, str | None, str | None]:
    """(form, entity_name, cik, role) from an EDGAR index title, best-effort."""
    m = _EDGAR_TITLE.match(title)
    if not m:
        return None, None, None, None
    form = m.group(1).upper().strip()
    return form, (m.group(2) or "").strip() or None, m.group(3), \
        (m.group(4) or "").strip() or None


def normalize_edgar(entry: dict, tier: int = 1) -> NewsItem:
    if not isinstance(entry, dict):
        raise NormalizeError("UNPARSEABLE_JSON", "non-object entry", raw_text=repr(entry)[:2000])

    title = str(_require(entry, "title")).strip()
    link = entry.get("link") or ""
    entry_id = str(entry.get("id") or "").strip()

    m = _ACCESSION.search(entry_id) or _ACCESSION.search(link)
    if m:
        item_id = f"edgar:{m.group(1)}"
    elif entry_id:
        item_id = f"edgar:{entry_id[-80:]}"
    else:
        raise NormalizeError("MISSING_REQUIRED_FIELD", "no accession number or entry id", raw=entry)

    updated = _ts_or_quarantine(entry, _require(entry, "updated"), "updated")

    channels = ["filing"]
    form, entity_name, cik, role = edgar_title_parts(title)
    if form:
        channels.append(f"form:{form}")
        if form.startswith("8-K"):
            channels.append("8-K")
    # Friday-after-close flag (baseline §4 C1): stamped as a channel so the
    # router/A1 see it without re-deriving.
    et = updated.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    if et.weekday() == 4 and et.hour >= 16:
        channels.append("friday_pm")

    summary = (entry.get("summary") or "").strip() or None

    return _build(
        entry,
        item_id=item_id,
        source="edgar",
        source_tier=tier,
        source_url=link or None,
        headline=title,
        summary=summary,
        content_hash=content_hash(title, summary),
        raw={
            **{k: str(v)[:2000] for k, v in entry.items()
               if isinstance(v, (str, int, float, bool))},
            "form": form or "",
            # entities: filled by the poller when merging multi-entity index
            # rows for one accession; single-entry fallback here.
            "entities": [{"name": entity_name or "", "cik": cik or "",
                          "role": role or ""}],
        },
        symbols=[],            # EDGAR entries carry CIK, not ticker; CIK->ticker mapping is the next fix
        channels=channels,
        published_ts=updated,
        received_ts=utcnow(),
    )


# ---------------------------------------------------------------------------
# Generic RSS entries (parsed by feedparser upstream)
# ---------------------------------------------------------------------------

def normalize_rss(entry: dict, feed_name: str, tier: int = 3) -> NewsItem:
    if not isinstance(entry, dict):
        raise NormalizeError("UNPARSEABLE_JSON", "non-object entry", raw_text=repr(entry)[:2000])

    title = str(_require(entry, "title")).strip()
    guid = str(entry.get("id") or entry.get("guid") or entry.get("link") or "").strip()
    if not guid:
        raise NormalizeError("MISSING_REQUIRED_FIELD", "no guid/link for stable item_id", raw=entry)

    published = entry.get("published") or entry.get("updated")
    if not published:
        raise NormalizeError("MISSING_REQUIRED_FIELD", "no published/updated timestamp", raw=entry)
    pub_ts = _ts_or_quarantine(entry, published, "published")

    summary = (entry.get("summary") or "").strip() or None
    # stable id: hash the guid so item_id length stays bounded
    import hashlib
    gid = hashlib.sha256(guid.encode()).hexdigest()[:24]

    return _build(
        entry,
        item_id=f"rss:{feed_name}:{gid}",
        source=f"rss:{feed_name}",
        source_tier=tier,
        source_url=entry.get("link") or None,
        author=entry.get("author") or None,
        headline=title,
        summary=summary,
        content_hash=content_hash(title, summary),
        raw={k: str(v)[:2000] for k, v in entry.items() if isinstance(v, (str, int, float, bool))},
        symbols=[],
        channels=[t.get("term", "") for t in entry.get("tags", []) if isinstance(t, dict)][:8],
        published_ts=pub_ts,
        received_ts=utcnow(),
    )

```

## `src/c1_ingestion/service.py`

```python
"""C1 ingestion service. One process, asyncio: one task per enabled source
plus a watchdog task that ticks every GapMonitor and refreshes the top-level
journal.health row. Sources that crash are restarted by their own run() loops
(Alpaca) or supervised here (pollers restart with the supervisor's backoff).

Run: python -m c1_ingestion.service            (with src/ on PYTHONPATH)
Config: config/sources.yaml, env per .env.example.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys

from common.db import close_pool
from common.log import get_logger, kv
from c1_ingestion.heartbeat import GapMonitor, set_health
from c1_ingestion.sources.alpaca_ws import AlpacaNewsSource
from c1_ingestion.sources.edgar import EdgarSource
from c1_ingestion.sources.rss import RssSource

log = get_logger("c1.service")

WATCHDOG_INTERVAL = 30.0


def load_sources_config() -> dict:
    from common.config import load_yaml, config_path
    return load_yaml(config_path("sources.yaml"))


async def _supervised(name: str, coro_factory, restart_delay: float = 5.0) -> None:
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("source task crashed; restarting",
                      extra=kv(source=name, error=repr(e)[:300], delay=restart_delay))
            await asyncio.sleep(restart_delay)


async def _watchdog(monitors: list[GapMonitor]) -> None:
    while True:
        for m in monitors:
            try:
                await m.check()
            except Exception as e:
                log.error("gap check failed", extra=kv(source=m.source, error=repr(e)[:200]))
        await set_health("ingestion", "OK", f"{len(monitors)} sources monitored")
        await asyncio.sleep(WATCHDOG_INTERVAL)


async def main() -> None:
    cfg = load_sources_config()
    tasks: list[asyncio.Task] = []
    monitors: list[GapMonitor] = []

    if cfg.get("alpaca", {}).get("enabled"):
        c = cfg["alpaca"]
        mon = GapMonitor("alpaca_benzinga", c["gap_threshold_market_secs"],
                         c["gap_threshold_offhours_secs"])
        monitors.append(mon)
        src = AlpacaNewsSource(c, mon)
        tasks.append(asyncio.create_task(_supervised("alpaca", src.run)))

    if cfg.get("edgar", {}).get("enabled"):
        c = cfg["edgar"]
        mon = GapMonitor("edgar", c["gap_threshold_market_secs"],
                         c["gap_threshold_offhours_secs"])
        monitors.append(mon)
        src = EdgarSource(c, mon)
        tasks.append(asyncio.create_task(_supervised("edgar", src.run)))

    if cfg.get("rss", {}).get("enabled"):
        c = cfg["rss"]
        mon = GapMonitor("rss", c["gap_threshold_market_secs"],
                         c["gap_threshold_offhours_secs"])
        monitors.append(mon)
        src = RssSource(c, mon)
        tasks.append(asyncio.create_task(_supervised("rss", src.run)))

    if not tasks:
        log.error("no sources enabled in sources.yaml")
        sys.exit(1)

    tasks.append(asyncio.create_task(_watchdog(monitors)))
    log.info("C1 up", extra=kv(sources=len(monitors)))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await set_health("ingestion", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

```

## `src/c1_ingestion/sources/__init__.py`

```python

```

## `src/c1_ingestion/sources/alpaca_ws.py`

```python
"""Alpaca news websocket (wss://stream.data.alpaca.markets/v1beta1/news).

Protocol:
  connect -> recv [{"T":"success","msg":"connected"}]
  send    {"action":"auth","key":K,"secret":S}
  recv    [{"T":"success","msg":"authenticated"}]
  send    {"action":"subscribe","news":["*"]}          # wildcard firehose (baseline)
  recv    [{"T":"subscription","news":["*"]}]
  then news frames: [{"T":"n", ...}, ...]

Reconnect: exponential backoff (base..max from sources.yaml) with jitter.
Every parse failure quarantines (UNPARSEABLE_JSON); every unknown frame type
is logged, not dropped silently. websockets' built-in ping/pong (20s default)
handles dead-TCP detection; the GapMonitor handles "connected but silent".
"""
from __future__ import annotations

import asyncio
import json
import os
import random

import websockets

from common.log import get_logger, kv
from c1_ingestion.heartbeat import GapMonitor, set_health
from c1_ingestion.normalize import NormalizeError, normalize_alpaca
from c1_ingestion.store import quarantine, store_item

log = get_logger("c1.alpaca")

COMPONENT = "ingestion:alpaca"


class AlpacaNewsSource:
    def __init__(self, cfg: dict, monitor: GapMonitor):
        self.url = os.environ.get("ALPACA_NEWS_WS", "wss://stream.data.alpaca.markets/v1beta1/news")
        self.key = os.environ.get("ALPACA_KEY_ID")
        self.secret = os.environ.get("ALPACA_SECRET_KEY")
        if not self.key or not self.secret:
            raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set (see .env.example)")
        self.tier = int(cfg.get("tier", 2))
        self.backoff_base = float(cfg.get("reconnect_base_secs", 1))
        self.backoff_max = float(cfg.get("reconnect_max_secs", 60))
        self.monitor = monitor

    async def run(self) -> None:
        backoff = self.backoff_base
        while True:
            try:
                await self._session()
                backoff = self.backoff_base          # clean close -> reset
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("session failed", extra=kv(error=repr(e)[:200]))
                await set_health(COMPONENT, "DEGRADED", f"reconnecting: {e!r}"[:200])
            sleep = min(backoff, self.backoff_max) * (0.5 + random.random())
            await asyncio.sleep(sleep)
            backoff = min(backoff * 2, self.backoff_max)

    async def _session(self) -> None:
        async with websockets.connect(self.url, max_size=2**22) as ws:
            await self._expect(ws, "connected")
            await ws.send(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
            await self._expect(ws, "authenticated")
            await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
            log.info("subscribed to wildcard firehose")
            await set_health(COMPONENT, "OK", "connected, wildcard subscribed")

            async for frame in ws:
                await self._handle_frame(frame)

    async def _expect(self, ws, msg: str) -> None:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        frames = json.loads(raw)
        for f in frames if isinstance(frames, list) else [frames]:
            if f.get("T") == "success" and f.get("msg") == msg:
                return
            if f.get("T") == "error":
                raise RuntimeError(f"alpaca error frame: {f}")
        raise RuntimeError(f"expected {msg!r}, got: {str(frames)[:200]}")

    async def _handle_frame(self, raw) -> None:
        try:
            frames = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            await quarantine(NormalizeError("UNPARSEABLE_JSON", str(e), raw_text=str(raw)[:2000]),
                             "alpaca_benzinga")
            return
        for f in frames if isinstance(frames, list) else [frames]:
            t = f.get("T")
            if t == "n":
                self.monitor.mark_activity()
                try:
                    item = normalize_alpaca(f, tier=self.tier)
                    await store_item(item)
                except NormalizeError as e:
                    await quarantine(e, "alpaca_benzinga")
            elif t in ("success", "subscription"):
                continue
            elif t == "error":
                log.error("stream error frame", extra=kv(frame=str(f)[:200]))
            else:
                log.warning("unknown frame type", extra=kv(T=t))

```

## `src/c1_ingestion/sources/edgar.py`

```python
"""SEC EDGAR current-events poller.

Fair-access compliance:
  * mandatory User-Agent "<EDGAR_APP_NAME> <EDGAR_CONTACT>" — fails fast at
    startup if EDGAR_CONTACT is unset (better than silent 403s at 2 AM)
  * poll_interval_secs from sources.yaml (default 15s, far under the
    10 req/s cap), one feed request per interval per feed
  * honors 429/503 with a longer sleep

Dedup across polls (fixed 2026-07-14 — the revision-storm incident):
  * EDGAR's index lists one filing once PER ASSOCIATED ENTITY (Filer /
    Subject / Filed-by rows share an accession). Entries are grouped by
    accession within each poll and merged into ONE item; all entities land
    in raw["entities"], the canonical headline prefers the Filer/Issuer row.
  * A filing is immutable by definition — store_item(immutable=True) makes
    any re-seen accession an unconditional no-op. No hash comparison, no
    revisions from this path, ever. Amended filings (8-K/A etc.) have new
    accession numbers -> new items, which is correct: an amendment is a new
    filing event, not a revision of the old text.
  * Form whitelist (config: edgar.triage_forms): only event-class filings
    enter the pipeline. Everything else is stored as a record with
    enqueue=False — kept for the archive, never costs dedup or triage
    inference. Matching is by form prefix so "8-K" admits "8-K/A".
"""
from __future__ import annotations

import asyncio
import os

import feedparser
import httpx

from common.log import get_logger, kv
from c1_ingestion.heartbeat import GapMonitor, set_health
from c1_ingestion.normalize import (NormalizeError, edgar_accession,
                                    edgar_title_parts, normalize_edgar)
from c1_ingestion.store import quarantine, store_item

log = get_logger("c1.edgar")

COMPONENT = "ingestion:edgar"

# Event-class filings worth triage inference; prefix match so "8-K" admits
# "8-K/A". Overridable via edgar.triage_forms in sources.yaml. 10-K/10-Q
# included for the long-horizon lane per baseline A5/A6.
DEFAULT_TRIAGE_FORMS = [
    "8-K", "6-K", "S-1", "425",
    "SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G",
    "10-K", "10-Q",
]

# Canonical-headline preference when merging multi-entity rows: the row
# naming the company (Filer/Issuer/Subject) over the person filing about it.
_ROLE_RANK = {"filer": 0, "issuer": 1, "subject": 2, "filed by": 3}


def _role_rank(role: str | None) -> int:
    return _ROLE_RANK.get((role or "").strip().lower(), 9)


def form_whitelisted(form: str | None, whitelist: list[str]) -> bool:
    if not form:
        return False
    f = form.upper().strip()
    return any(f == w or f.startswith(w + "/") or f.startswith(w + " ")
               for w in (w.upper().strip() for w in whitelist))


def user_agent() -> str:
    contact = os.environ.get("EDGAR_CONTACT")
    if not contact:
        raise RuntimeError("EDGAR_CONTACT not set — SEC fair-access policy requires "
                           "a contact email in the User-Agent (see .env.example)")
    app = os.environ.get("EDGAR_APP_NAME", "Trading System")
    return f"{app} {contact}"


class EdgarSource:
    def __init__(self, cfg: dict, monitor: GapMonitor):
        self.tier = int(cfg.get("tier", 1))
        self.interval = float(cfg.get("poll_interval_secs", 15))
        self.feeds = [{"name": "8-K-current", "url": cfg["feed_url"]}]
        self.feeds += list(cfg.get("extra_feeds", []))
        self.triage_forms = list(cfg.get("triage_forms", DEFAULT_TRIAGE_FORMS))
        self.monitor = monitor
        self.ua = user_agent()

    async def run(self) -> None:
        async with httpx.AsyncClient(
            headers={"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"},
            timeout=20.0, follow_redirects=True,
        ) as client:
            await set_health(COMPONENT, "OK", f"polling every {self.interval}s")
            while True:
                for feed in self.feeds:
                    try:
                        await self._poll(client, feed)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.error("poll failed", extra=kv(feed=feed["name"], error=repr(e)[:200]))
                        await set_health(COMPONENT, "DEGRADED", f"{feed['name']}: {e!r}"[:200])
                    await asyncio.sleep(1.0)      # spacing between feeds within a cycle
                await asyncio.sleep(self.interval)

    async def _poll(self, client: httpx.AsyncClient, feed: dict) -> None:
        resp = await client.get(feed["url"])
        if resp.status_code in (429, 503):
            log.warning("rate limited", extra=kv(feed=feed["name"], status=resp.status_code))
            await asyncio.sleep(60)
            return
        resp.raise_for_status()

        parsed = feedparser.parse(resp.text)
        if parsed.bozo and not parsed.entries:
            await quarantine(NormalizeError("UNPARSEABLE_JSON",
                                            f"atom parse: {parsed.bozo_exception!r}",
                                            raw_text=resp.text[:2000]), "edgar")
            return

        # Group index rows by accession: one filing = one item, however many
        # associated-entity rows the index shows for it.
        groups: dict[str, list[dict]] = {}
        ungrouped: list[dict] = []
        for entry in parsed.entries:
            e = dict(entry)
            acc = edgar_accession(e)
            if acc:
                groups.setdefault(acc, []).append(e)
            else:
                ungrouped.append(e)          # normalize_edgar falls back to entry id

        stored = skipped_form = 0
        for acc, entries in groups.items():
            try:
                item = self._merge_group(entries)
                allow = form_whitelisted((item.raw or {}).get("form"), self.triage_forms)
                result = await store_item(item, immutable=True, enqueue=allow)
                if result.stored:
                    stored += 1
                    if not allow:
                        skipped_form += 1
                    self.monitor.mark_activity()
            except NormalizeError as e:
                await quarantine(e, "edgar")
        for e in ungrouped:
            try:
                item = normalize_edgar(e, tier=self.tier)
                allow = form_whitelisted((item.raw or {}).get("form"), self.triage_forms)
                result = await store_item(item, immutable=True, enqueue=allow)
                if result.stored:
                    stored += 1
                    self.monitor.mark_activity()
            except NormalizeError as e2:
                await quarantine(e2, "edgar")

        if stored:
            log.info("poll stored", extra=kv(feed=feed["name"], new=stored,
                                             archived_only=skipped_form))
        # a successful poll is liveness even with zero new filings
        self.monitor.mark_activity()

    def _merge_group(self, entries: list[dict]):
        """One NewsItem per accession. Canonical row = best role rank (the
        company over the person); all entity rows preserved in raw."""
        ranked = sorted(entries, key=lambda e: _role_rank(
            edgar_title_parts(str(e.get("title") or ""))[3]))
        item = normalize_edgar(ranked[0], tier=self.tier)
        entities = []
        for e in ranked:
            _, name, cik, role = edgar_title_parts(str(e.get("title") or ""))
            ent = {"name": name or "", "cik": cik or "", "role": role or ""}
            if ent not in entities:
                entities.append(ent)
        if item.raw is not None:
            item.raw["entities"] = entities
        return item

```

## `src/c1_ingestion/sources/rss.py`

```python
"""Generic RSS poller (Tier 3). Conditional GET via ETag/Last-Modified where
the feed supports it; per-poll dedup is inherent via item_id + content_hash
(store_item no-ops echoes). Like EDGAR, a successful poll marks liveness —
the gap we track for pollers is "cannot fetch", not "publisher quiet".
"""
from __future__ import annotations

import asyncio

import feedparser
import httpx

from common.log import get_logger, kv
from c1_ingestion.heartbeat import GapMonitor, set_health
from c1_ingestion.normalize import NormalizeError, normalize_rss
from c1_ingestion.store import quarantine, store_item

log = get_logger("c1.rss")

COMPONENT = "ingestion:rss"


class RssSource:
    def __init__(self, cfg: dict, monitor: GapMonitor):
        self.tier = int(cfg.get("tier", 3))
        self.interval = float(cfg.get("poll_interval_secs", 60))
        self.feeds = list(cfg.get("feeds", []))
        self.monitor = monitor
        self._cache: dict[str, dict] = {}     # feed name -> {etag, last_modified}

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                     headers={"User-Agent": "news-pipeline/0.1"}) as client:
            await set_health(COMPONENT, "OK", f"{len(self.feeds)} feeds, every {self.interval}s")
            while True:
                for feed in self.feeds:
                    try:
                        await self._poll(client, feed)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.error("poll failed", extra=kv(feed=feed["name"], error=repr(e)[:200]))
                        await set_health(COMPONENT, "DEGRADED", f"{feed['name']}: {e!r}"[:200])
                    await asyncio.sleep(0.5)
                await asyncio.sleep(self.interval)

    async def _poll(self, client: httpx.AsyncClient, feed: dict) -> None:
        name, url = feed["name"], feed["url"]
        headers = {}
        cache = self._cache.get(name, {})
        if cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        if cache.get("last_modified"):
            headers["If-Modified-Since"] = cache["last_modified"]

        resp = await client.get(url, headers=headers)
        if resp.status_code == 304:
            self.monitor.mark_activity()
            return
        resp.raise_for_status()
        self._cache[name] = {"etag": resp.headers.get("ETag"),
                             "last_modified": resp.headers.get("Last-Modified")}

        parsed = feedparser.parse(resp.text)
        if parsed.bozo and not parsed.entries:
            await quarantine(NormalizeError("UNPARSEABLE_JSON",
                                            f"rss parse: {parsed.bozo_exception!r}",
                                            raw_text=resp.text[:2000]), f"rss:{name}")
            return

        stored = 0
        for entry in parsed.entries:
            try:
                item = normalize_rss(dict(entry), feed_name=name, tier=self.tier)
                result = await store_item(item)
                if result.stored:
                    stored += 1
            except NormalizeError as e:
                await quarantine(e, f"rss:{name}")
        if stored:
            log.info("poll stored", extra=kv(feed=name, new=stored))
        self.monitor.mark_activity()

```

## `src/c1_ingestion/store.py`

```python
"""C1 storage. The critical invariant: the news_items insert and the
signal.dedup enqueue happen in ONE transaction — a crash between "stored" and
"enqueued" is impossible (rule 19's at-least-once starts here).

Revision logic (v0.4 corrections):
  * new item_id                          -> revision 1
  * existing item_id, same content_hash  -> no-op (feed replay/reconnect echo)
  * existing item_id, changed hash       -> revision N+1, supersedes=N,
                                            is_correction=True
Dedup key for the queue is "{item_id}:{revision}" per spec §3, so each
revision flows through the pipeline exactly once.

Immutable sources (2026-07-14 EDGAR revision-storm fix): for sources whose
identity key already guarantees content identity (EDGAR accession numbers —
a filing never changes; amendments are new accessions), the caller passes
immutable=True and ANY re-seen item_id is a no-op, hash comparison skipped.
Rationale: EDGAR's index lists one filing once per associated entity
(Filer/Subject/Filed-by rows). Per-entity rows alternate content under the
same accession, defeating latest-hash comparison and minting a revision on
every poll cycle (observed: rev 58+ on a single 13G, ~80k items/day).
Hash-based revisioning remains the default for genuinely revisable sources.

enqueue=False stores the item as a record without entering it into the
pipeline (form-whitelist down-routing: Form 4 / 424B2 etc. are kept for the
archive but never cost dedup or triage inference).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from common.contracts import NewsItem, envelope, CONTRACT_DEDUPED
from common.db import get_pool, jb
from common.log import get_logger, kv
from common.queue import enqueue as _enqueue

from .normalize import NormalizeError

log = get_logger("c1.store")

DEDUP_QUEUE = "signal.dedup"


@dataclass
class StoreResult:
    stored: bool                 # False = duplicate echo, nothing written
    revision: int
    is_correction: bool
    enqueued: bool


async def store_item(item: NewsItem, *, immutable: bool = False,
                     enqueue: bool = True) -> StoreResult:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            # Lock the item's revision history to serialize concurrent revisions
            cur = await conn.execute(
                """SELECT revision, content_hash FROM news.news_items
                   WHERE item_id = %s ORDER BY revision DESC LIMIT 1
                   FOR UPDATE""",
                (item.item_id,),
            )
            latest = await cur.fetchone()

            if latest is None:
                revision, supersedes, is_corr = 1, None, item.is_correction
            else:
                prev_rev, prev_hash = latest
                if immutable or prev_hash == item.content_hash:
                    return StoreResult(stored=False, revision=prev_rev,
                                       is_correction=False, enqueued=False)
                revision, supersedes, is_corr = prev_rev + 1, prev_rev, True

            await conn.execute(
                """INSERT INTO news.news_items
                   (item_id, revision, is_correction, supersedes, source, source_tier,
                    source_url, author, headline, summary, content_hash, raw,
                    symbols, channels, lang, published_ts, received_ts)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (item.item_id, revision, is_corr, supersedes, item.source,
                 item.source_tier, item.source_url, item.author, item.headline,
                 item.summary, item.content_hash,
                 jb(item.raw) if item.raw is not None else None,
                 item.symbols, item.channels, item.lang,
                 item.published_ts, item.received_ts),
            )

            enqueued = False
            if enqueue:
                body = item.model_copy(update={
                    "revision": revision, "is_correction": is_corr, "supersedes": supersedes,
                }).payload()
                msg = envelope(CONTRACT_DEDUPED, "C1", item.item_id, item.item_id,
                               revision, body)
                enqueued = await _enqueue(DEDUP_QUEUE, f"{item.item_id}:{revision}",
                                          msg, conn=conn)

    log.info("stored", extra=kv(item_id=item.item_id, revision=revision,
                                correction=is_corr, enqueued=enqueued))
    return StoreResult(stored=True, revision=revision, is_correction=is_corr, enqueued=enqueued)


async def quarantine(err: NormalizeError, source: str) -> None:
    """v0.4: malformed input is kept, never dropped. C7 alerts on rate spikes."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO news.quarantine (source, reason_code, detail, raw, raw_text)
               VALUES (%s,%s,%s,%s,%s)""",
            (source, err.reason_code, err.detail,
             jb(err.raw) if err.raw is not None else None, err.raw_text),
        )
    log.warning("quarantined", extra=kv(source=source, reason=err.reason_code, detail=err.detail))

```

## `src/c2_dedup/__init__.py`

```python

```

## `src/c2_dedup/cluster.py`

```python
"""C2 dedup + cluster decision logic.

For each incoming item revision:
  1. embed headline+summary
  2. nearest neighbor in dedup_48h
  3.   sim >= similarity_threshold (0.90) AND same story already seen
         -> DUPLICATE: join the neighbor's cluster, refresh corroboration,
            forward to A1 only as is_new_story=false (spec §5: A1 invoked only
            if corroboration crossing thresholds matters — that's the router's
            call in Phase 2; C2 always forwards the signal with the flag)
  4.   cluster_threshold (0.80) <= sim < similarity_threshold
         -> RELATED: same story, distinct enough wording to be corroboration
            from another outlet; join cluster, is_new_story=false
  5.   sim < cluster_threshold -> NEW STORY: create cluster, is_new_story=true

A *revision* of an item already in a cluster stays in its cluster (the story
identity didn't change; the text did) — membership rows are per revision.

Corroboration counting (independent outlets) is the cluster_corroboration
view in Postgres; we read it back after every membership write so the
DedupedSignal always carries current numbers (C3's credibility input, v0.2).
"""
from __future__ import annotations

from dataclasses import dataclass

from common.db import get_pool
from common.log import get_logger, kv

from .embedder import embed_text_for
from .vectorstore import VectorStore

log = get_logger("c2.cluster")


@dataclass
class ClusterDecision:
    cluster_id: int
    is_new_story: bool
    independent_outlets: int
    total_items: int
    similarity_to_canonical: float
    # >= similarity_threshold to an existing DIFFERENT item: baseline §4 C2
    # says drop. Membership + corroboration are still recorded (C3 reads the
    # view); the signal just doesn't re-enter the pipeline. Fixed 2026-07-14 —
    # C2 previously computed this verdict, logged it, and forwarded anyway.
    is_duplicate: bool = False


async def _existing_cluster_of(item_id: str) -> int | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT cluster_id FROM news.cluster_members WHERE item_id = %s LIMIT 1",
            (item_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def _create_cluster(canonical_item: str) -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO news.clusters (canonical_item) VALUES (%s) RETURNING cluster_id",
            (canonical_item,))
        return (await cur.fetchone())[0]


async def _add_member(cluster_id: int, item_id: str, revision: int,
                      source: str, similarity: float) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO news.cluster_members
               (cluster_id, item_id, revision, source, similarity)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (cluster_id, item_id, revision) DO NOTHING""",
            (cluster_id, item_id, revision, source, similarity))


async def _corroboration(cluster_id: int) -> tuple[int, int]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT independent_outlets, total_items
               FROM news.cluster_corroboration WHERE cluster_id = %s""",
            (cluster_id,))
        row = await cur.fetchone()
        return (row[0], row[1]) if row else (1, 1)


class Deduper:
    def __init__(self, store: VectorStore, embedder, similarity_threshold: float = 0.90,
                 cluster_threshold: float = 0.80):
        self.store = store
        self.embedder = embedder
        self.sim_threshold = similarity_threshold
        self.cluster_threshold = cluster_threshold

    async def process(self, item: dict) -> ClusterDecision:
        """item: the NewsItem payload dict from the signal.dedup message body."""
        item_id, revision = item["item_id"], item["revision"]
        source = item["source"]
        vector = self.embedder.embed(embed_text_for(item["headline"], item.get("summary")))

        # A revision of a clustered item stays in its cluster.
        existing = await _existing_cluster_of(item_id)
        if existing is not None:
            await _add_member(existing, item_id, revision, source, 1.0)
            self.store.upsert_dedup(item_id, revision, vector, existing, source)
            outlets, total = await _corroboration(existing)
            log.info("revision joined own cluster",
                     extra=kv(item_id=item_id, rev=revision, cluster=existing))
            return ClusterDecision(existing, False, outlets, total, 1.0,
                                   is_duplicate=False)

        neighbors = [n for n in self.store.nearest(vector, limit=3)
                     if not (n.item_id == item_id)]
        best = neighbors[0] if neighbors else None

        is_dup = False
        if best is not None and best.score >= self.cluster_threshold and best.cluster_id:
            cluster_id, is_new = best.cluster_id, False
            sim = float(best.score)
            is_dup = best.score >= self.sim_threshold
            kind = "duplicate" if is_dup else "corroboration"
            log.info(f"joined cluster ({kind})",
                     extra=kv(item_id=item_id, cluster=cluster_id, sim=round(sim, 3)))
        else:
            cluster_id, is_new, sim = await _create_cluster(item_id), True, 1.0
            log.info("new story cluster", extra=kv(item_id=item_id, cluster=cluster_id))

        await _add_member(cluster_id, item_id, revision, source, sim)
        self.store.upsert_dedup(item_id, revision, vector, cluster_id, source)
        outlets, total = await _corroboration(cluster_id)
        return ClusterDecision(cluster_id, is_new, outlets, total, sim,
                               is_duplicate=is_dup)

```

## `src/c2_dedup/embedder.py`

```python
"""Pluggable embedder. EMBEDDER=bge -> sentence-transformers
BAAI/bge-small-en-v1.5 (384-dim; install the [embed] extra and let it download
the model — one-time, ~130MB). EMBEDDER=hash -> deterministic 384-dim bag-of-
token-hashes embedder: no downloads, stable across runs, near-duplicate texts
map to near-identical vectors. Dev/test only — its notion of similarity is
lexical, not semantic, so it under-clusters paraphrases. Production uses bge.

Both are L2-normalized so Qdrant cosine similarity is a dot product and the
0.9 dedup threshold means the same thing in either mode.
"""
from __future__ import annotations

import hashlib
import math
import os
import re

DIM = 384

_TOKEN = re.compile(r"[a-z0-9]{2,}")


class HashEmbedder:
    """Deterministic: sha1(token) -> bucket + sign, L2-normalized."""
    name = "hash-384"

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * DIM
        for tok in _TOKEN.findall(text.lower()):
            h = hashlib.sha1(tok.encode()).digest()
            bucket = int.from_bytes(h[:4], "big") % DIM
            sign = 1.0 if h[4] % 2 == 0 else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class BgeEmbedder:
    name = "bge-small-en-v1.5"

    def __init__(self):
        from sentence_transformers import SentenceTransformer  # [embed] extra
        self._model = SentenceTransformer("BAAI/bge-small-en-v1.5")

    def embed(self, text: str) -> list[float]:
        return self._model.encode(text, normalize_embeddings=True).tolist()


def get_embedder():
    kind = os.environ.get("EMBEDDER", "hash").lower()
    if kind == "bge":
        return BgeEmbedder()
    if kind == "hash":
        return HashEmbedder()
    raise RuntimeError(f"unknown EMBEDDER={kind!r} (expected 'bge' or 'hash')")


def embed_text_for(headline: str, summary: str | None) -> str:
    """What we embed: headline + summary. Body is noisy and slow; the
    dedup/cluster decision is a story-identity question, which the headline
    and lede carry."""
    return headline if not summary else f"{headline}\n{summary}"

```

## `src/c2_dedup/service.py`

```python
"""C2 dedup service. Consumes signal.dedup, runs the dedup/cluster decision,
and enqueues a DedupedSignal (spec §5) on signal.triage. Consumer-side dedup
is inherent: signal.triage's dedup_key is the same "{item_id}:{revision}", so
an at-least-once redelivery of a dedup message re-enqueues as a no-op.

Failed messages route through queue.fail() -> linear backoff -> DLQ into
news.quarantine after max_attempts (spec §1); the consumer never crashes on a
bad message.
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal

from common.contracts import (CONTRACT_TRIAGE, ClusterInfo, DedupedSignal, envelope)
from common.db import close_pool
from common.log import get_logger, kv
from common.queue import ack, claim, enqueue, fail, wait_for_message
from c1_ingestion.heartbeat import set_health

from .cluster import Deduper
from .embedder import get_embedder
from .vectorstore import VectorStore

log = get_logger("c2.service")

DEDUP_QUEUE = "signal.dedup"
TRIAGE_QUEUE = "signal.triage"
CONSUMER = f"c2-{os.getpid()}"
PRUNE_INTERVAL = 3600.0
WINDOW_HOURS = 48


async def handle_message(msg, deduper: Deduper) -> None:
    body = msg.payload.get("body") or {}
    trace = (msg.payload.get("envelope") or {}).get("trace") or {}
    item_id = body.get("item_id") or trace.get("item_id")
    revision = body.get("revision") or trace.get("revision") or 1
    if not item_id or not body.get("headline"):
        raise ValueError(f"malformed dedup message: missing item fields ({msg.dedup_key})")

    decision = await deduper.process(body)

    # Baseline §4 C2: >= 0.90 similarity to something already seen -> drop.
    # Corroboration/membership were recorded in process(); the anti-
    # overtrading requirement (principle 8) means the duplicate must not
    # re-trigger triage. (Fix 2026-07-14: was forwarding after detection.)
    if decision.is_duplicate:
        log.info("duplicate dropped",
                 extra=kv(item_id=item_id, rev=revision,
                          cluster=decision.cluster_id,
                          sim=round(decision.similarity_to_canonical, 3)))
        return

    ds = DedupedSignal(item=body, cluster=ClusterInfo(
        cluster_id=decision.cluster_id,
        is_new_story=decision.is_new_story,
        independent_outlets=decision.independent_outlets,
        total_items=decision.total_items,
        similarity_to_canonical=round(decision.similarity_to_canonical, 4),
    ))
    out = envelope(CONTRACT_TRIAGE, "C2", item_id, item_id, revision,
                   ds.model_dump())
    await enqueue(TRIAGE_QUEUE, f"{item_id}:{revision}", out)
    log.info("forwarded to triage",
             extra=kv(item_id=item_id, rev=revision, cluster=decision.cluster_id,
                      new_story=decision.is_new_story,
                      outlets=decision.independent_outlets))


async def consume_loop(deduper: Deduper, stop: asyncio.Event) -> None:
    await set_health("dedup", "OK", "consuming signal.dedup")
    while not stop.is_set():
        msg = await claim(DEDUP_QUEUE, CONSUMER)
        if msg is None:
            # idle: block on NOTIFY with a poll fallback
            try:
                await asyncio.wait_for(wait_for_message(DEDUP_QUEUE, timeout_secs=5.0), 6.0)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await handle_message(msg, deduper)
            await ack(msg.msg_id)
        except Exception as e:
            log.error("message failed", extra=kv(msg_id=msg.msg_id, error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def prune_loop(store: VectorStore, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            store.prune_dedup(WINDOW_HOURS)
        except Exception as e:
            log.error("prune failed", extra=kv(error=repr(e)[:200]))
        try:
            await asyncio.wait_for(stop.wait(), timeout=PRUNE_INTERVAL)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    embedder = get_embedder()
    store = VectorStore()
    deduper = Deduper(store, embedder)
    log.info("C2 up", extra=kv(embedder=embedder.name, consumer=CONSUMER))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    await asyncio.gather(consume_loop(deduper, stop), prune_loop(store, stop))
    await set_health("dedup", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

```

## `src/c2_dedup/vectorstore.py`

```python
"""Qdrant wrapper implementing the v0.4 two-collection rule:

  dedup_48h  — every item's vector, pruned to a trailing 48h window (hourly)
  retrieval  — material items only, long retention; admission is A1's material
               flag, so Phase 1 only *implements* promote_to_retrieval() —
               A1's wrapper calls it in Phase 2.

QDRANT_URL set   -> server mode (the Spark's docker container)
QDRANT_URL empty -> qdrant-client local mode persisted at QDRANT_PATH
Identical API either way — the mode is a deployment detail.

Point IDs: uuid5 of "{item_id}:{revision}" (Qdrant requires uuid/int IDs);
the original ids ride in the payload.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (Distance, FieldCondition, Filter, PointStruct,
                                  Range, VectorParams)

from common.clock import utcnow
from common.log import get_logger, kv

log = get_logger("c2.vectorstore")

_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def _pid(item_id: str, revision: int) -> str:
    return str(uuid.uuid5(_NS, f"{item_id}:{revision}"))


@dataclass
class Neighbor:
    item_id: str
    revision: int
    cluster_id: Optional[int]
    score: float


class VectorStore:
    def __init__(self, dedup_collection: str = "dedup_48h",
                 retrieval_collection: str = "retrieval", dim: int = 384,
                 path: str | None = None, url: str | None = None):
        url = url or os.environ.get("QDRANT_URL") or None
        if url:
            self.client = QdrantClient(url=url)
            mode = url
        else:
            path = path or os.environ.get("QDRANT_PATH", "./qdrant-local")
            self.client = QdrantClient(path=path)
            mode = f"local:{path}"
        self.dedup = dedup_collection
        self.retrieval = retrieval_collection
        self.dim = dim
        for coll in (self.dedup, self.retrieval):
            if not self.client.collection_exists(coll):
                self.client.create_collection(
                    coll, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
        log.info("vector store ready", extra=kv(mode=mode, dim=dim))

    # -- dedup collection ----------------------------------------------------

    def nearest(self, vector: list[float], limit: int = 5) -> list[Neighbor]:
        hits = self.client.query_points(self.dedup, query=vector, limit=limit).points
        return [Neighbor(item_id=h.payload["item_id"], revision=h.payload["revision"],
                         cluster_id=h.payload.get("cluster_id"), score=h.score)
                for h in hits]

    def upsert_dedup(self, item_id: str, revision: int, vector: list[float],
                     cluster_id: int, source: str) -> None:
        self.client.upsert(self.dedup, points=[PointStruct(
            id=_pid(item_id, revision), vector=vector,
            payload={"item_id": item_id, "revision": revision,
                     "cluster_id": cluster_id, "source": source,
                     "ts": utcnow().timestamp()},
        )])

    def prune_dedup(self, window_hours: int = 48) -> int:
        """Trailing-window prune — keeps the collection small and fast forever."""
        cutoff = (utcnow() - timedelta(hours=window_hours)).timestamp()
        flt = Filter(must=[FieldCondition(key="ts", range=Range(lt=cutoff))])
        before = self.client.count(self.dedup, count_filter=flt).count
        if before:
            self.client.delete(self.dedup, points_selector=flt)
            log.info("pruned dedup collection", extra=kv(removed=before))
        return before

    # -- retrieval collection (admission = A1 material flag; Phase 2 caller) --

    def promote_to_retrieval(self, item_id: str, revision: int, vector: list[float],
                             payload: dict) -> None:
        """Copy a material item into the long-retention retrieval collection.
        Called by A1's wrapper when it sets material=true (Phase 2)."""
        self.client.upsert(self.retrieval, points=[PointStruct(
            id=_pid(item_id, revision), vector=vector,
            payload={"item_id": item_id, "revision": revision, **payload},
        )])

    def related(self, vector: list[float], limit: int = 8) -> list[dict]:
        """Related-headline context for A2 (Phase 3 caller) — retrieval only."""
        hits = self.client.query_points(self.retrieval, query=vector, limit=limit).points
        return [{"score": h.score, **h.payload} for h in hits]

```

## `src/c3_gate/__init__.py`

```python

```

## `src/c3_gate/rules.py`

```python
"""C3 Market Confirmation Gate rules (code — the primary anti-overtrading
control). Pure functions over a MarketState snapshot; the service does I/O.

Check order (cheapest first, all journaled on veto):
  1. LONG_ONLY        direction != "up" -> no entry path exists (long-only book)
  2. CREDIBILITY      corroboration matrix: required independent outlets =
                      f(impact bucket, source tier); Tier-1 passes alone;
                      source_risk="high" raises the requirement one level
  3. intraday vs open-handoff branch on whether the news arrived in-session:
     intraday:  GATE_WINDOW    minutes_since_publish > N
                GATE_EXTENDED  already >= extended_pct from pre-news
                GATE_NO_CONFIRM pct_move < X or vol_mult < Y
     handoff:   GATE_OPEN_WINDOW first 15 minutes after open
                PRICED_IN      gap >= gap_ratio * magnitude_est

All thresholds from config/gate.yaml — PLACEHOLDER values pending the §14
gate-threshold design item; the rule SHAPES are per baseline v0.5.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class MarketState:
    """Everything the rules need, computed by the service from market data."""
    prenews_price: float
    last_price: float
    vol_mult: Optional[float]          # since-news minute volume / baseline
    minutes_since_publish: int
    news_in_session: bool              # published during RTH -> intraday rule
    minutes_since_open: Optional[int]  # None when market closed
    gap_pct: Optional[float]           # today's open vs prev close (handoff)
    corroboration_outlets: int
    tier_min: int                      # best (lowest) tier in the cluster


@dataclass
class GateVerdict:
    verdict: str                       # PASS | VETO
    rule: str                          # intraday | open_handoff
    veto_reason: Optional[str] = None
    numbers: dict | None = None        # journaled either way


def _impact_bucket(magnitude_est: float, cfg: dict) -> str:
    if magnitude_est >= cfg["impact_high_min"]:
        return "high"
    if magnitude_est >= cfg["impact_medium_min"]:
        return "medium"
    return "low"


def credibility_required(impact: str, tier_min: int, source_risk: str,
                         cfg: dict) -> int:
    """Required independent outlets. Tier-1 filing passes alone (returns 1).
    High source_risk bumps the impact bucket one level."""
    if tier_min == 1:
        return 1
    order = ["low", "medium", "high"]
    if source_risk == "high":
        impact = order[min(order.index(impact) + 1, 2)]
    return int(cfg["required_outlets"][impact][tier_min])


def evaluate(thesis: dict, state: MarketState, cfg: dict) -> GateVerdict:
    pct_move = ((state.last_price - state.prenews_price) / state.prenews_price
                if state.prenews_price else 0.0)
    numbers = {"pct_move": round(pct_move, 5), "vol_mult": state.vol_mult,
               "minutes": state.minutes_since_publish,
               "gap_pct": state.gap_pct,
               "corroboration": {"independent_outlets": state.corroboration_outlets,
                                 "tier_min": state.tier_min}}
    rule = "intraday" if state.news_in_session else "open_handoff"

    # 1. long-only
    if thesis["direction"] != "up":
        return GateVerdict("VETO", rule, "LONG_ONLY", numbers)

    # 2. credibility
    impact = _impact_bucket(float(thesis["magnitude_est"]), cfg)
    required = credibility_required(impact, state.tier_min,
                                    thesis["source_risk"], cfg)
    numbers["credibility"] = {"impact": impact, "required_outlets": required}
    if state.corroboration_outlets < required:
        return GateVerdict("VETO", rule, "CREDIBILITY", numbers)

    # 3a. intraday confirmation
    if rule == "intraday":
        if state.minutes_since_publish > cfg["intraday_window_min"]:
            return GateVerdict("VETO", rule, "GATE_WINDOW", numbers)
        if pct_move >= cfg["extended_pct"]:
            return GateVerdict("VETO", rule, "GATE_EXTENDED", numbers)
        if pct_move < cfg["intraday_move_pct"] or not state.vol_mult \
                or state.vol_mult < cfg["intraday_vol_mult"]:
            return GateVerdict("VETO", rule, "GATE_NO_CONFIRM", numbers)
        return GateVerdict("PASS", rule, None, numbers)

    # 3b. open handoff
    if state.minutes_since_open is None or state.minutes_since_open < cfg["open_blackout_min"]:
        return GateVerdict("VETO", rule, "GATE_OPEN_WINDOW", numbers)
    if state.gap_pct is not None and \
            state.gap_pct >= cfg["handoff_gap_ratio"] * float(thesis["magnitude_est"]):
        return GateVerdict("VETO", rule, "PRICED_IN", numbers)
    # small gap on rated news = the opportunity; still demand some confirmation
    if pct_move >= cfg["extended_pct"]:
        return GateVerdict("VETO", rule, "GATE_EXTENDED", numbers)
    return GateVerdict("PASS", rule, None, numbers)

```

## `src/c3_gate/service.py`

```python
"""C3 Market Confirmation Gate service (Phase 3, observe-only downstream —
signal.risk accumulates until Phase 4's A3).

Per message on signal.gate (§7):
  1. compute MarketState from market data + the news store cluster tables
  2. rules.evaluate() -> PASS | VETO
  3. VETO: journal GATE decision with veto_reason; STOP (no message — §8)
     PASS: ONE TRANSACTION: GATE decision + GatePass (§8) on signal.risk,
     snapshot included (A3's limit-pricing basis, copied to intents later)
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal
from datetime import datetime, timedelta, timezone

from common.clock import parse_ts, utcnow
from common.config import config_path, load_yaml
from common.contracts import envelope
from common.db import close_pool, get_pool
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from common.marketdata import adv20, atr14, avg_minute_volume, get_marketdata
from common.queue import ack, claim, enqueue, fail, wait_for_message
from c1_ingestion.heartbeat import set_health
from router.facts import market_open_now, _schedule_cache

from .rules import GateVerdict, MarketState, evaluate

log = get_logger("c3.service")

IN_QUEUE = "signal.gate"
OUT_QUEUE = "signal.risk"
CONSUMER = f"c3-{os.getpid()}"
CONTRACT_GATEPASS = "signal.risk/1"


def _session_window(ts: datetime) -> tuple[datetime, datetime] | None:
    """NYSE session bounds for ts's date (uses the router's cached calendar)."""
    import pandas_market_calendars as mcal
    day_key = ts.strftime("%Y-%m-%d")
    if day_key not in _schedule_cache:
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=day_key, end_date=day_key)
        _schedule_cache[day_key] = None if sched.empty else (
            sched.iloc[0]["market_open"].to_pydatetime(),
            sched.iloc[0]["market_close"].to_pydatetime())
    return _schedule_cache[day_key]


async def _corroboration(item_id: str) -> tuple[int, int]:
    """(independent_outlets, tier_min) for the item's cluster."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT cm.cluster_id FROM news.cluster_members cm
               WHERE cm.item_id = %s LIMIT 1""", (item_id,))
        row = await cur.fetchone()
        if row is None:
            return 1, 3
        cluster_id = row[0]
        cur = await conn.execute(
            """SELECT c.independent_outlets, min(ni.source_tier)
               FROM news.cluster_corroboration c
               JOIN news.cluster_members cm ON cm.cluster_id = c.cluster_id
               JOIN news.news_items ni ON ni.item_id = cm.item_id
                                       AND ni.revision = cm.revision
               WHERE c.cluster_id = %s
               GROUP BY c.independent_outlets""", (cluster_id,))
        row = await cur.fetchone()
        return (row[0], row[1]) if row else (1, 3)


class C3Service:
    def __init__(self, cfg: dict, md=None, now_fn=None):
        self.cfg = cfg["gate"]
        self.md = md or get_marketdata()
        self.now_fn = now_fn or utcnow

    async def build_state(self, thesis: dict, item_id: str,
                          published_ts: datetime, now: datetime) -> MarketState:
        ticker = thesis["ticker"]
        quote = await self.md.snapshot(ticker)
        prev = await self.md.prev_close(ticker)

        pre_bars = await self.md.minute_bars(
            ticker, published_ts - timedelta(minutes=30), published_ts)
        prenews = pre_bars[-1]["close"] if pre_bars else prev

        since = await self.md.minute_bars(ticker, published_ts, now)
        baseline = await self.md.minute_bars(
            ticker, published_ts - timedelta(days=5), published_ts)
        b_vol, s_vol = avg_minute_volume(baseline), avg_minute_volume(since)
        vol_mult = round(s_vol / b_vol, 2) if (s_vol and b_vol) else None

        pub_session = _session_window(published_ts)
        news_in_session = bool(pub_session and
                               pub_session[0] <= published_ts < pub_session[1])
        today_session = _session_window(now)
        minutes_since_open = None
        if today_session and now >= today_session[0]:
            minutes_since_open = int((now - today_session[0]).total_seconds() // 60)

        gap_pct = None
        if not news_in_session:
            day_bars = await self.md.minute_bars(
                ticker, today_session[0], now) if today_session else []
            if day_bars and prev:
                gap_pct = round((day_bars[0]["open"] - prev) / prev, 5)

        outlets, tier_min = await _corroboration(item_id)
        return MarketState(
            prenews_price=prenews, last_price=quote.price, vol_mult=vol_mult,
            minutes_since_publish=int((now - published_ts).total_seconds() // 60),
            news_in_session=news_in_session,
            minutes_since_open=minutes_since_open, gap_pct=gap_pct,
            corroboration_outlets=outlets, tier_min=tier_min)

    async def handle(self, msg) -> None:
        body = msg.payload.get("body") or {}
        thesis = body.get("thesis") or {}
        item_ref = body.get("item_ref") or {}
        item_id = item_ref.get("item_id")
        revision = int(item_ref.get("revision") or 1)
        signal_id = (msg.payload.get("envelope", {}).get("trace", {})
                     .get("signal_id") or item_id)
        if not item_id or not thesis.get("ticker"):
            raise ValueError(f"malformed thesis message ({msg.dedup_key})")

        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT published_ts FROM news.news_items WHERE item_id=%s AND revision=%s",
                (item_id, revision))
            row = await cur.fetchone()
        if row is None:
            raise ValueError(f"item not in news store: {item_id} rev {revision}")
        published_ts = row[0]

        now = self.now_fn()
        state = await self.build_state(thesis, item_id, published_ts, now)
        verdict = evaluate(thesis, state, self.cfg)

        if verdict.verdict == "VETO":
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                ticker=thesis["ticker"], stage="GATE", agent="C3",
                action="VETO", veto_reason=verdict.veto_reason,
                payload={"rule": verdict.rule, **(verdict.numbers or {})},
                reason=f"{verdict.veto_reason} ({verdict.rule})",
                regime_id=body.get("regime_id"))
            log.info("gate VETO", extra=kv(signal_id=signal_id,
                                           reason=verdict.veto_reason,
                                           rule=verdict.rule))
            return

        quote = await self.md.snapshot(thesis["ticker"])
        daily = await self.md.daily_bars(thesis["ticker"], 30)
        snapshot = {"ref_price": quote.price, "bid": quote.bid, "ask": quote.ask,
                    "spread_bps": quote.spread_bps, "adv_20d": adv20(daily),
                    "atr_14": atr14(daily), "ts": quote.ts.isoformat()}
        gate_body = {"thesis": thesis,
                     "gate": {"verdict": "PASS", "rule": verdict.rule,
                              **(verdict.numbers or {}), "snapshot": snapshot}}

        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=signal_id, item_id=item_id, item_revision=revision,
                    ticker=thesis["ticker"], stage="GATE", agent="C3",
                    action="PASS",
                    payload=gate_body["gate"],
                    reason=f"confirmed ({verdict.rule})",
                    regime_id=body.get("regime_id"), conn=conn)
                out = envelope(CONTRACT_GATEPASS, "C3", signal_id, item_id,
                               revision, gate_body)
                out["envelope"]["trace"]["decision_id"] = decision_id
                await enqueue(OUT_QUEUE, f"{signal_id}:{revision}", out, conn=conn)

        log.info("gate PASS", extra=kv(signal_id=signal_id,
                                       ticker=thesis["ticker"], rule=verdict.rule))


async def consume_loop(svc: C3Service, stop: asyncio.Event) -> None:
    await set_health("gate", "OK", f"consuming {IN_QUEUE}")
    while not stop.is_set():
        msg = await claim(IN_QUEUE, CONSUMER)
        if msg is None:
            try:
                await asyncio.wait_for(wait_for_message(IN_QUEUE, timeout_secs=5.0), 6.0)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await svc.handle(msg)
            await ack(msg.msg_id)
        except Exception as e:
            log.error("message failed", extra=kv(msg_id=msg.msg_id, error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def main() -> None:
    cfg = load_yaml(config_path("gate.yaml"))
    await register_config_version("c3 gate service startup")
    svc = C3Service(cfg)
    log.info("C3 up", extra=kv(consumer=CONSUMER))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await consume_loop(svc, stop)
    await set_health("gate", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

```

## `src/c4_exec/__init__.py`

```python

```

## `src/c4_exec/breaker.py`

```python
"""Drawdown breaker (phase4-design-v1_0 D7-confirmed: -2% daily).

Day PnL = realized (exits journaled today, UTC session date) + unrealized
(open positions marked at last_price). Trip when day PnL <= -2% of effective
capital: set drawdown_breaker=1 (audited, actor C4). ONE-WAY — code never
resets it; the operator does, from the dashboard, deliberately (runbook §5).
A3 vetoes and C4 pre-flight both already honor the flag; exits continue.
"""
from __future__ import annotations

from common.db import get_pool
from common.log import get_logger, kv

from .flags import breaker_on, get_flag, set_flag

log = get_logger("c4.breaker")


async def day_pnl() -> dict:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT COALESCE(sum(realized_pnl),0) FROM journal.exits
               WHERE ts::date = (now() AT TIME ZONE 'UTC')::date""")
        realized = float((await cur.fetchone())[0])
        cur = await conn.execute(
            """SELECT COALESCE(sum((last_price - avg_entry) * qty_open),0)
               FROM journal.positions
               WHERE status='OPEN' AND last_price IS NOT NULL""")
        unrealized = float((await cur.fetchone())[0])
    return {"realized": realized, "unrealized": unrealized,
            "total": realized + unrealized}


async def check_breaker(drawdown_pct: float) -> bool:
    """Returns True if the breaker is (now) tripped."""
    if await breaker_on():
        return True
    equity = float(await get_flag("broker_equity", "0") or 0)
    capital = float(await get_flag("trading_capital", "0") or 0)
    effective = min(equity, capital)
    if effective <= 0:
        return False
    pnl = await day_pnl()
    threshold = -drawdown_pct * effective
    if pnl["total"] <= threshold:
        await set_flag("drawdown_breaker", "1", "C4",
                       f"BREAKER_TRIP day_pnl={pnl['total']:.2f} "
                       f"(realized={pnl['realized']:.2f} "
                       f"unrealized={pnl['unrealized']:.2f}) "
                       f"threshold={threshold:.2f}")
        log.warning("drawdown breaker TRIPPED",
                    extra=kv(day_pnl=round(pnl["total"], 2),
                             threshold=round(threshold, 2)))
        return True
    return False

```

## `src/c4_exec/deadman.py`

```python
"""Dead-man switch monitor (phase4-design-v1_0 D4).

Reads journal.health heartbeat timestamps; applies the ladder from
config/deadman.yaml. Escalation is ALERT -> BLOCK_ENTRIES -> (marketdata
only) exit-engine suspend. NEVER auto-flatten: catastrophe stops are
broker-resident precisely so that a dead pipeline leaves protected
positions, and a panicked robot selling into an outage is worse than one
that stands still.

Ownership rule: the monitor only CLEARS blocks it set itself (control key
deadman_block='1' marks ownership) — an operator's manual block_entries is
never unwound by code. Runs inside C4's monitor task; RTH-only for
escalations, ALERT-only off-hours.
"""
from __future__ import annotations

from datetime import datetime, timezone

from common.db import get_pool
from common.log import get_logger, kv
from c1_ingestion.heartbeat import set_health

from .flags import get_flag, set_flag

log = get_logger("monitor.deadman")

COMPONENT_MAP = {"ingestion": "ingestion", "marketdata": "marketdata",
                 "triage": "triage_model", "analyst": "analyst_model",
                 "gate": "gate"}


async def heartbeat_ages(now: datetime) -> dict[str, float]:
    """Minutes since each component's last health update."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT component, updated_ts FROM journal.health")
        rows = await cur.fetchall()
    ages = {}
    for component, ts in rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ages[component] = (now - ts).total_seconds() / 60.0
    return ages


async def check(cfg: dict, now: datetime, in_session: bool) -> dict:
    """One monitor pass. Returns the actions taken (for tests/logging)."""
    ages = await heartbeat_ages(now)
    actions = {"alerts": [], "block": False, "unblock": False,
               "exit_suspend": False, "exit_resume": False}
    want_block = False
    want_exit_suspend = False

    for name, thresholds in cfg["components"].items():
        component = COMPONENT_MAP.get(name, name)
        age = ages.get(component)
        if age is None:
            continue                      # component never started — cold start
        if age > thresholds["alert_min"]:
            actions["alerts"].append((component, round(age, 1)))
        if in_session and "block_entries_min" in thresholds \
                and age > thresholds["block_entries_min"]:
            want_block = True
        if in_session and "exit_engine_suspend_min" in thresholds \
                and age > thresholds["exit_engine_suspend_min"]:
            want_exit_suspend = True

    deadman_owns = await get_flag("deadman_block") == "1"
    blocked = await get_flag("block_entries") == "1"

    if want_block and not blocked:
        await set_flag("block_entries", "1", "DEADMAN",
                       f"heartbeat stale: {actions['alerts']}")
        await set_flag("deadman_block", "1", "DEADMAN")
        actions["block"] = True
        log.warning("dead-man BLOCK_ENTRIES", extra=kv(alerts=actions["alerts"]))
    elif not want_block and blocked and deadman_owns:
        await set_flag("block_entries", "0", "DEADMAN", "heartbeats recovered")
        await set_flag("deadman_block", "0", "DEADMAN")
        actions["unblock"] = True
        log.info("dead-man unblock: heartbeats recovered")

    exit_suspended = await get_flag("exit_engine_suspended") == "1"
    if want_exit_suspend and not exit_suspended:
        await set_flag("exit_engine_suspended", "1", "DEADMAN",
                       "marketdata stale >suspend threshold: catastrophe "
                       "stops are sole protection")
        actions["exit_suspend"] = True
        log.error("EXIT ENGINE SUSPENDED — catastrophe stops sole protection")
    elif not want_exit_suspend and exit_suspended:
        await set_flag("exit_engine_suspended", "0", "DEADMAN",
                       "marketdata recovered")
        actions["exit_resume"] = True

    for component, age in actions["alerts"]:
        await set_health("deadman", "DEGRADED",
                         f"stale: {component} {age}min")
    if not actions["alerts"]:
        await set_health("deadman", "OK", "all heartbeats fresh")
    return actions

```

## `src/c4_exec/engine.py`

```python
"""C4 exit engine loop (Phase 4 chunk 2).

PositionEngine owns the live per-position state that must survive across
bars: compiled MIP monitors (persistence streaks are stateful) and halt
freeze status. One engine instance runs inside the C4 service; tests drive
step()/overnight_pass() directly with synthetic bars and a pinned clock.

Responsibilities per bar: mark-to-market cache -> halt heuristic ->
MIP on_bar (exit fires feed the evaluator; tighten_stop/alert_guard fires
become ratchets/guard events) -> evaluate_on_bar -> apply actions through
mechanics (never unprotected beyond the configured window).
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from common.clock import utcnow
from common.db import get_pool, jb
from common.invalidation_dsl import ArmContext, Bar, compile_predicate
from common.log import get_logger, kv

from .exits import ExitAction, evaluate_on_bar, policy_state
from .mechanics import execute_exit
from .overnight import overnight_decision, realized_move_fraction
from .state import open_positions, position_event

log = get_logger("c4.engine")
ET = ZoneInfo("America/New_York")

_session_cache: dict = {}


def sessions_between(opened_ts: datetime, now: datetime) -> int:
    """Completed sessions since entry (0 on the entry session)."""
    import pandas_market_calendars as mcal
    key = (opened_ts.date().isoformat(), now.date().isoformat())
    if key not in _session_cache:
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=key[0], end_date=key[1])
        _session_cache[key] = max(len(sched) - 1, 0)
    return _session_cache[key]


class PositionEngine:
    def __init__(self, broker, now_fn=None, unprotected_max_secs: float = 45.0,
                 poll_sleep: float = 1.0, halt_stale_min: float = 10.0,
                 session_age_fn=None):
        self.broker = broker
        self.now_fn = now_fn or utcnow
        self.unprotected_max_secs = unprotected_max_secs
        self.poll_sleep = poll_sleep
        self.halt_stale_min = halt_stale_min
        self.session_age_fn = session_age_fn or sessions_between
        self.monitors: dict[int, list] = {}       # position_id -> predicates
        self.frozen: set[int] = set()
        self.last_bar_ts: dict[int, datetime] = {}

    # ------------------------------------------------------------------ arming
    async def arm(self, pos: dict) -> None:
        """Compile machine invalidations once per position; journal the
        compiled literal forms (INVALIDATION_ARMED)."""
        pid = pos["position_id"]
        if pid in self.monitors:
            return
        policy = pos["exit_policy"]
        specs = policy.get("machine_invalidations") or []
        compiled = []
        armed_forms = []
        ctx = ArmContext(entry_price=float(pos["avg_entry"]),
                         initial_stop=float(pos["initial_stop"]),
                         r_unit=float(pos["r_unit"]),
                         prenews_price=policy.get("prenews_price")
                         and float(policy["prenews_price"]),
                         atr_14=policy.get("atr_14") and float(policy["atr_14"]),
                         mark=pos.get("last_price") and float(pos["last_price"]))
        for spec in specs:
            if isinstance(spec, str):
                spec = {"std": spec}
            try:
                p = compile_predicate(spec, ctx)
                compiled.append(p)
                armed_forms.append(p.compiled_form)
            except Exception as e:
                await position_event(pid, "INVALIDATION_ARMED", "C4",
                                     new_value={"spec": spec},
                                     detail=f"ARM FAILED: {repr(e)[:150]}")
                log.warning("predicate arm failed",
                            extra=kv(position_id=pid, error=repr(e)[:150]))
        self.monitors[pid] = compiled
        if armed_forms:
            await position_event(pid, "INVALIDATION_ARMED", "C4",
                                 new_value={"predicates": armed_forms},
                                 detail=f"{len(armed_forms)} armed")

    # -------------------------------------------------------------------- bars
    async def step(self, pos: dict, bar: dict) -> list[str]:
        """Process one minute bar for one open position. Returns applied
        action descriptions (test/observability)."""
        pid = pos["position_id"]
        now = self.now_fn()
        await self.arm(pos)
        self.last_bar_ts[pid] = now
        if pid in self.frozen:
            self.frozen.discard(pid)
            await position_event(pid, "HALT_RESUMED", "C4",
                                 detail="bar flow resumed")

        await self._mark(pid, bar["close"])
        pos = {**pos, "last_price": bar["close"]}

        # MIP monitors
        mip_bar = Bar(ts=int(bar.get("ts", now.timestamp())),
                      tf=bar.get("tf", "1m"),
                      open=bar["open"], high=bar["high"], low=bar["low"],
                      close=bar["close"], vwap=bar.get("vwap", bar["close"]),
                      volume_ratio=bar.get("volume_ratio", 1.0))
        exit_fires, extra_actions = [], []
        for p in self.monitors.get(pid, []):
            fire = p.on_bar(mip_bar)
            if fire is None:
                continue
            await position_event(pid, "INVALIDATION_FIRED", "C4",
                                 new_value={"predicate": fire.predicate_id,
                                            "action": fire.action},
                                 detail=fire.detail[:200])
            if fire.action.get("type") == "exit":
                exit_fires.append(fire)
            elif fire.action.get("type") == "tighten_stop":
                extra_actions.append(self._tighten_from_fire(pos, fire))
            else:                                     # alert_guard
                extra_actions.append(ExitAction(
                    "EVENT", "", 0, event_type="GUARD_ACTION",
                    reason=f"alert_guard: {fire.predicate_id}"))

        session_age = self.session_age_fn(pos["opened_ts"], now)
        actions = evaluate_on_bar(pos, bar, session_age, exit_fires)
        actions.extend(a for a in extra_actions if a is not None)
        return await self._apply(pos, actions, bar)

    def _tighten_from_fire(self, pos: dict, fire) -> Optional[ExitAction]:
        to = fire.action.get("to", {})
        policy = pos["exit_policy"]
        state = policy_state(policy, float(pos["avg_entry"]))
        if to == {"ref": "breakeven"}:
            new_stop = round(float(pos["avg_entry"]), 2)
            basis = "breakeven"
        else:
            atr = float(policy["atr_14"])
            new_stop = round(state["hwm"] - float(to["atr_k"]) * atr, 2)
            basis = "trail"
        if new_stop <= state["current_stop"]:
            return None                               # tighten-only
        return ExitAction("SET_STOP", "", 0, new_stop=new_stop,
                          new_basis=basis,
                          reason=f"MIP tighten_stop {fire.predicate_id}")

    async def check_halt(self, pos: dict) -> bool:
        """True if the position is (now) frozen: no bar within the stale
        window during RTH. LULD heuristic-only until SIP (D7)."""
        pid = pos["position_id"]
        last = self.last_bar_ts.get(pid)
        if last is None:
            return False
        stale_min = (self.now_fn() - last).total_seconds() / 60.0
        if stale_min > self.halt_stale_min and pid not in self.frozen:
            self.frozen.add(pid)
            await position_event(pid, "HALT_FROZEN", "C4",
                                 detail=f"no bar for {stale_min:.1f}min — "
                                        f"halt heuristic; evaluations frozen")
            log.warning("halt heuristic froze position",
                        extra=kv(position_id=pid, ticker=pos["ticker"]))
        return pid in self.frozen

    # ------------------------------------------------------------------- apply
    async def _apply(self, pos: dict, actions: list[ExitAction],
                     bar: dict) -> list[str]:
        applied = []
        for a in actions:
            if a.kind in ("EXIT", "SCALE_OUT"):
                bid = bar.get("bid") or round(bar["close"] * 0.999, 2)
                outcome = await execute_exit(
                    self.broker, pos, a.qty, a.layer, a.reason, bid,
                    self.now_fn, self.unprotected_max_secs, self.poll_sleep)
                applied.append(f"{a.kind}:{a.layer}:{outcome}")
                if a.kind == "EXIT" or outcome == "CATASTROPHE_FILLED":
                    self.monitors.pop(pos["position_id"], None)
                    break                             # position closed
            elif a.kind == "SET_STOP":
                await self._ratchet(pos, a)
                applied.append(f"SET_STOP:{a.new_basis}:{a.new_stop}")
            elif a.kind == "EVENT":
                if a.event_type == "POSITION_REVIEW":
                    await position_event(pos["position_id"], "SCALE_OUT", "C4",
                                         detail=f"REVIEW_FLAG: {a.reason}",
                                         new_value={"review": True})
                    applied.append("EVENT:REVIEW")
                elif a.event_type == "GUARD_ACTION":
                    await position_event(pos["position_id"], "GUARD_ACTION",
                                         "C4", detail=a.reason)
                    applied.append("EVENT:GUARD")
                if a.new_hwm is not None:
                    await self._update_policy(pos, {"hwm": a.new_hwm})
        return applied

    async def _ratchet(self, pos: dict, a: ExitAction) -> None:
        policy = pos["exit_policy"]
        state = policy_state(policy, float(pos["avg_entry"]))
        event_type = {"breakeven": "BREAKEVEN_MOVED",
                      "trail": "TRAIL_UPDATED"}.get(a.new_basis,
                                                    "STOP_TIGHTENED")
        updates = {"current_stop": a.new_stop, "stop_basis": a.new_basis}
        if a.new_hwm is not None:
            updates["hwm"] = a.new_hwm
        await self._update_policy(pos, updates)
        r_prog = ((float(pos.get("last_price") or pos["avg_entry"]))
                  - float(pos["avg_entry"])) / float(pos["r_unit"])
        await position_event(pos["position_id"], event_type, "C4",
                             old_value={"stop": state["current_stop"],
                                        "basis": state["stop_basis"]},
                             new_value={"stop": a.new_stop,
                                        "basis": a.new_basis},
                             r_progress=round(r_prog, 3), detail=a.reason)

    async def _update_policy(self, pos: dict, updates: dict) -> None:
        pos["exit_policy"].update(updates)
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """UPDATE journal.positions SET exit_policy=%s
                   WHERE position_id=%s""",
                (jb(pos["exit_policy"]), pos["position_id"]))

    async def _mark(self, position_id: int, price: float) -> None:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """UPDATE journal.positions SET last_price=%s, last_price_ts=%s
                   WHERE position_id=%s""",
                (price, self.now_fn(), position_id))

    async def session_close_pass(self, daily_bar_fn) -> None:
        """After the close: feed each open position its completed session bar
        so session-tf MIP predicates (e.g. close_below_prenews) can evaluate.
        daily_bar_fn(ticker) -> {open,high,low,close} of the finished session."""
        for pos in await open_positions():
            b = await daily_bar_fn(pos["ticker"])
            if not b:
                continue
            await self.step(pos, {**b, "tf": "session"})

    # ---------------------------------------------------------------- overnight
    async def overnight_pass(self, cfg: dict, earnings_fn=None,
                             pass_label: str = "15:45") -> list[tuple]:
        """D1: one decision per open SHORT position; EXIT -> limit at bid.
        Run at 15:45 ET and again at 15:55 (reprice pass) — the second pass
        re-attempts only positions still open. Returns [(ticker, decision,
        rule)] for tests."""
        results = []
        for pos in await open_positions():
            if pos["horizon"] != "SHORT":
                continue
            policy = pos["exit_policy"]
            avg_entry = float(pos["avg_entry"])
            mark = float(pos.get("last_price") or avg_entry)
            unrealized_r = (mark - avg_entry) / float(pos["r_unit"])
            age = self.session_age_fn(pos["opened_ts"], self.now_fn())
            frac = realized_move_fraction(mark, avg_entry,
                                          float(policy.get("magnitude_est") or 0))
            earn = await earnings_fn(pos["ticker"]) if earnings_fn else None
            decision, rule = overnight_decision(unrealized_r, age, frac,
                                                earn, cfg)
            await position_event(
                pos["position_id"], "OVERNIGHT_HOLD_DECISION", "C4",
                new_value={"decision": decision, "rule": rule,
                           "unrealized_R": round(unrealized_r, 3),
                           "session_age": age,
                           "realized_fraction": round(frac, 3),
                           "pass": pass_label},
                r_progress=round(unrealized_r, 3),
                detail=f"{decision} ({rule}) @ {pass_label}")
            if decision == "EXIT":
                bid = round(mark * 0.999, 2)
                outcome = await execute_exit(
                    self.broker, pos, int(pos["qty_open"]), "OVERNIGHT",
                    f"D1 {rule}", bid, self.now_fn,
                    self.unprotected_max_secs, self.poll_sleep)
                if outcome == "REINSTATED" and pass_label == "15:55":
                    await position_event(
                        pos["position_id"], "OVERNIGHT_HOLD_DECISION", "C4",
                        new_value={"decision": "FORCED_HOLD"},
                        detail="OVERNIGHT_FORCED_HOLD: unfilled after reprice; "
                               "holding with catastrophe intact")
            results.append((pos["ticker"], decision, rule))
        return results

```

## `src/c4_exec/exits.py`

```python
"""C4 exit engine — pure per-bar evaluator (Phase 4 chunk 2).

Layers per baseline v0.3 §5, evaluated in strict priority on each bar:
  L1 synthetic hard stop   bar.low <= current_stop -> full exit. Attribution
                           follows stop_basis: initial->STOP, breakeven->
                           BREAKEVEN, trail->TRAIL.
  L5 machine invalidation  compiled MIP predicates fire -> full exit
                           (INVALIDATION). Runs second: an invalidation on the
                           same bar as a stop is moot — the stop already got us.
  L3 time stop             session age >= window AND progress < min_progress_R
                           -> full exit (TIME). Short profile only.
  L4 realization           bar.high >= target -> scale_out_50 (TARGET, partial)
                           or review_flag (EVENT only, long lane).
  L2 ratchets              breakeven move at >= breakeven_at_R; trail from
                           activate_at_R at k x ATR below high-water mark.
                           TIGHTEN-ONLY: a proposed stop below current is
                           discarded, never applied.

The evaluator is pure: (position snapshot, bar, session_age, fired
invalidations) -> ordered actions. All broker mechanics live in mechanics.py;
all persistence in the service. Runtime policy state rides in exit_policy:
current_stop, stop_basis, hwm, scale_out_done.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExitAction:
    kind: str                      # EXIT | SCALE_OUT | SET_STOP | EVENT
    layer: str = ""                # exits.exit_layer vocabulary
    qty: int = 0
    reason: str = ""
    new_stop: Optional[float] = None
    new_basis: Optional[str] = None
    event_type: Optional[str] = None
    new_hwm: Optional[float] = None


STOP_ATTRIBUTION = {"initial": "STOP", "breakeven": "BREAKEVEN",
                    "trail": "TRAIL"}


def policy_state(exit_policy: dict, avg_entry: float) -> dict:
    """Current runtime state with defaults for freshly opened positions."""
    return {
        "current_stop": float(exit_policy.get("current_stop")
                              or exit_policy["initial_stop"]["price"]),
        "stop_basis": exit_policy.get("stop_basis", "initial"),
        "hwm": float(exit_policy.get("hwm") or avg_entry),
        "scale_out_done": bool(exit_policy.get("scale_out_done", False)),
    }


def realization_target(avg_entry: float, exit_policy: dict) -> float:
    rf = float(exit_policy["realization"]["target_fraction"])
    magnitude = float(exit_policy.get("magnitude_est") or 0)
    return round(avg_entry * (1.0 + rf * magnitude), 4)


def evaluate_on_bar(pos: dict, bar: dict, session_age: int,
                    fired_invalidations: list | None = None
                    ) -> list[ExitAction]:
    """pos: positions row as dict (exit_policy already parsed).
    bar: {ts, open, high, low, close} floats.
    session_age: completed sessions since entry (0 = entry session).
    fired_invalidations: Fire objects from the compiled MIP monitors for
    this bar (the caller runs the DSL; the evaluator stays pure)."""
    policy = pos["exit_policy"]
    avg_entry = float(pos["avg_entry"])
    r_unit = float(pos["r_unit"])
    qty_open = int(pos["qty_open"])
    state = policy_state(policy, avg_entry)
    actions: list[ExitAction] = []

    progress_r = (bar["close"] - avg_entry) / r_unit if r_unit else 0.0
    new_hwm = max(state["hwm"], bar["high"])

    # ---- L1 synthetic hard stop ------------------------------------------------
    if bar["low"] <= state["current_stop"]:
        layer = STOP_ATTRIBUTION[state["stop_basis"]]
        actions.append(ExitAction("EXIT", layer, qty_open,
                                  reason=f"bar low {bar['low']} <= stop "
                                         f"{state['current_stop']} "
                                         f"({state['stop_basis']})"))
        return actions

    # ---- L5 machine invalidations ----------------------------------------------
    if fired_invalidations:
        f = fired_invalidations[0]
        actions.append(ExitAction("EXIT", "INVALIDATION", qty_open,
                                  reason=f"{f.predicate_id}: {f.detail}"[:200]))
        return actions

    # ---- L3 time stop ------------------------------------------------------------
    ts_cfg = policy.get("time_stop")
    if ts_cfg:
        window = int(str(ts_cfg["window"]).split("_")[0])
        if session_age >= window and progress_r < float(ts_cfg["min_progress_R"]):
            actions.append(ExitAction(
                "EXIT", "TIME", qty_open,
                reason=f"age {session_age}s >= {window}s window, "
                       f"progress {progress_r:.2f}R < "
                       f"{ts_cfg['min_progress_R']}R"))
            return actions

    # ---- L4 realization ------------------------------------------------------------
    if not state["scale_out_done"]:
        target = realization_target(avg_entry, policy)
        if bar["high"] >= target:
            action = policy["realization"]["action"]
            if action == "scale_out_50":
                half = qty_open // 2
                if half > 0:
                    actions.append(ExitAction("SCALE_OUT", "TARGET", half,
                                              reason=f"high {bar['high']} >= "
                                                     f"target {target}"))
            else:                               # review_flag (long lane)
                actions.append(ExitAction(
                    "EVENT", "TARGET", 0, event_type="POSITION_REVIEW",
                    reason=f"target {target} reached — review flagged"))

    # ---- L2 ratchets (tighten-only) ---------------------------------------------
    atr = float(policy["atr_14"])
    proposed: Optional[tuple[float, str]] = None
    trail_cfg = policy.get("trail") or {}
    if trail_cfg and progress_r >= float(trail_cfg["activate_at_R"]):
        trail_stop = round(new_hwm - float(trail_cfg["k"]) * atr, 2)
        proposed = (trail_stop, "trail")
    elif progress_r >= float(policy["breakeven_at_R"]) \
            and state["stop_basis"] == "initial":
        proposed = (round(avg_entry, 2), "breakeven")

    if proposed and proposed[0] > state["current_stop"]:
        actions.append(ExitAction("SET_STOP", "", 0,
                                  new_stop=proposed[0], new_basis=proposed[1],
                                  reason=f"{proposed[1]} ratchet to "
                                         f"{proposed[0]} at {progress_r:.2f}R",
                                  new_hwm=new_hwm))
    elif new_hwm > state["hwm"]:
        actions.append(ExitAction("EVENT", "", 0, event_type=None,
                                  new_hwm=new_hwm, reason="hwm update"))
    return actions

```

## `src/c4_exec/flags.py`

```python
"""Operational controls + audit (phase4-design-v1_0 D2/D4 + C6 contract).

journal.control is the single flag store: the dashboard flips values, ONLY
code enforces them. C4 checks kill_switch before every submission; A3 reads
the same rows at sizing. Every mutation writes an audit row.

Keys: kill_switch, drawdown_breaker, block_entries ('0'/'1'),
trading_capital, max_trades_per_day (numbers as text),
broker_equity, settled_cash, last_reconcile_ts (C4-written, read-only to UI).
"""
from __future__ import annotations

from common.db import get_pool
from common.log import get_logger, kv

log = get_logger("c4.flags")

DEFAULTS = {"kill_switch": "0", "drawdown_breaker": "0", "block_entries": "0",
            "max_trades_per_day": "5"}


async def ensure_defaults() -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        for k, v in DEFAULTS.items():
            await conn.execute(
                """INSERT INTO journal.control (key, value, updated_ts)
                   VALUES (%s, %s, now()) ON CONFLICT (key) DO NOTHING""", (k, v))


async def get_flag(key: str, default: str = "0") -> str:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT value FROM journal.control WHERE key=%s", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_flag(key: str, value: str, actor: str, detail: str = "",
                   conn=None) -> None:
    async def _run(c):
        cur = await c.execute(
            "SELECT value FROM journal.control WHERE key=%s", (key,))
        row = await cur.fetchone()
        old = row[0] if row else None
        await c.execute(
            """INSERT INTO journal.control (key, value, updated_ts)
               VALUES (%s,%s,now())
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                                               updated_ts=now()""", (key, value))
        await c.execute(
            """INSERT INTO journal.audit (actor, action, old_value, new_value, detail)
               VALUES (%s,%s,%s,%s,%s)""",
            (actor, f"{key.upper()}_SET", old, value, detail[:300]))
    if conn is not None:
        await _run(conn)
    else:
        pool = await get_pool()
        async with pool.connection() as c:
            await _run(c)
    log.info("control set", extra=kv(key=key, value=value, actor=actor))


async def kill_switch_on() -> bool:
    return await get_flag("kill_switch") == "1"


async def breaker_on() -> bool:
    return await get_flag("drawdown_breaker") == "1"


async def entries_blocked() -> bool:
    """Any of the three entry-blocking flags (the D4 ladder's BLOCK_ENTRIES
    plus kill and breaker) — C4's single pre-submission check."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT key, value FROM journal.control
               WHERE key IN ('kill_switch','drawdown_breaker','block_entries')""")
        rows = await cur.fetchall()
    return any(v == "1" for _, v in rows)

```

## `src/c4_exec/mechanics.py`

```python
"""C4 exit mechanics (phase4-design-v1_0 D3).

Every synthetic-layer exit follows the same sequence — the position is never
knowingly unprotected for more than exit_unprotected_max_secs:

  1. cancel the broker-resident catastrophe stop (journal the cancel)
  2. submit the exit as a marketable limit at the bid
  3. poll: filled -> exits row (layer attribution) + events; if SCALE_OUT,
     re-place the catastrophe for the remaining shares
  4. NOT filled within the window -> cancel the exit, REINSTATE the
     catastrophe stop, journal EXIT_REINSTATED — the exit attempt failed but
     the position is protected again; the next bar re-evaluates.

Catastrophe fills found at the broker (tier-1 fired on its own) are recorded
by record_catastrophe_fill during the poll loop / reconciliation.
"""
from __future__ import annotations

import uuid
from typing import Optional

from common.broker import Broker, BrokerReject
from common.clock import utcnow
from common.db import get_pool
from common.log import get_logger, kv

from .state import (create_order, position_event, record_exit,
                    transition_order)

log = get_logger("c4.mechanics")


async def _current_catastrophe(position_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT o.order_id, o.broker_order_id, o.qty, o.stop_price
               FROM journal.orders o
               JOIN journal.positions p
                 ON p.catastrophe_stop_order_id = o.order_id
               WHERE p.position_id=%s""", (position_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        return {"order_id": row[0], "broker_order_id": row[1],
                "qty": row[2], "stop_price": float(row[3])}


async def _place_catastrophe(broker: Broker, pos: dict, qty: int,
                             stop_price: float) -> int:
    o = await broker.submit_stop(pos["ticker"], "SELL", qty, stop_price,
                                 client_order_id=f"cat-{uuid.uuid4().hex[:12]}")
    order_row = await create_order(None, "CATASTROPHE_STOP", o,
                                   position_id=pos["position_id"])
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """UPDATE journal.positions SET catastrophe_stop_order_id=%s
               WHERE position_id=%s""", (order_row, pos["position_id"]))
    return order_row


async def execute_exit(broker: Broker, pos: dict, qty: int, layer: str,
                       reason: str, bid: float, now_fn,
                       unprotected_max_secs: float = 45.0,
                       poll_sleep: float = 1.0,
                       sleep_fn=None) -> str:
    """Returns 'FILLED' | 'REINSTATED' | 'CATASTROPHE_FILLED'."""
    import asyncio
    sleep = sleep_fn or asyncio.sleep
    position_id = pos["position_id"]
    is_partial = qty < int(pos["qty_open"])

    cat = await _current_catastrophe(position_id)
    if cat is not None:
        cancelled = await broker.cancel(cat["broker_order_id"])
        if not cancelled:
            # cancel failed because the stop is terminal OR unknown: the
            # catastrophe may have FILLED broker-side — check, but a broker
            # that can't even find the order must not crash the engine
            try:
                co = await broker.get_order(cat["broker_order_id"])
            except Exception:
                co = None
                await position_event(position_id, "GUARD_ACTION", "C4",
                                     detail="catastrophe order unknown at "
                                            "broker during exit — proceeding "
                                            "with exit, protection state "
                                            "uncertain")
            if co is not None and co.status == "filled":
                await transition_order(cat["order_id"], co)
                await record_exit(position_id, cat["order_id"], now_fn(),
                                  "CATASTROPHE", co.filled_qty,
                                  float(co.filled_avg_price),
                                  float(pos["avg_entry"]),
                                  float(pos["r_unit"]), is_partial=False)
                log.warning("catastrophe had already filled",
                            extra=kv(position_id=position_id))
                return "CATASTROPHE_FILLED"

    try:
        exit_order = await broker.submit_limit(
            pos["ticker"], "SELL", qty, round(bid, 2),
            client_order_id=f"exit-{uuid.uuid4().hex[:12]}", tif="day")
    except BrokerReject as e:
        # protect first, diagnose second
        if cat is not None:
            await _place_catastrophe(broker, pos, int(pos["qty_open"]),
                                     cat["stop_price"])
        await position_event(position_id, "GUARD_ACTION", "C4",
                             detail=f"exit submit rejected: {e}", )
        return "REINSTATED"

    order_row = await create_order(None, "EXIT" if not is_partial
                                   else "SCALE_OUT", exit_order,
                                   position_id=position_id)
    waited = 0.0
    while waited < unprotected_max_secs:
        o = await broker.get_order(exit_order.broker_order_id)
        if o.status == "filled":
            await transition_order(order_row, o)
            await record_exit(position_id, order_row, now_fn(), layer,
                              o.filled_qty, float(o.filled_avg_price),
                              float(pos["avg_entry"]), float(pos["r_unit"]),
                              is_partial=is_partial)
            remaining = int(pos["qty_open"]) - o.filled_qty
            if remaining > 0 and cat is not None:
                await _place_catastrophe(broker, pos, remaining,
                                         cat["stop_price"])
            log.info("exit filled", extra=kv(position_id=position_id,
                                             layer=layer, qty=o.filled_qty,
                                             price=o.filled_avg_price))
            return "FILLED"
        await sleep(poll_sleep)
        waited += poll_sleep

    # window expired: cancel exit, reinstate protection
    await broker.cancel(exit_order.broker_order_id)
    final = await broker.get_order(exit_order.broker_order_id)
    await transition_order(order_row, final)
    if final.status == "filled":                     # raced the cancel
        await record_exit(position_id, order_row, now_fn(), layer,
                          final.filled_qty, float(final.filled_avg_price),
                          float(pos["avg_entry"]), float(pos["r_unit"]),
                          is_partial=is_partial)
        return "FILLED"
    if cat is not None:
        await _place_catastrophe(broker, pos, int(pos["qty_open"]),
                                 cat["stop_price"])
    await position_event(position_id, "GUARD_ACTION", "C4",
                         detail=f"EXIT_REINSTATED: {layer} exit unfilled in "
                                f"{unprotected_max_secs}s, catastrophe re-placed",
                         new_value={"layer": layer, "qty": qty})
    log.warning("exit reinstated", extra=kv(position_id=position_id,
                                            layer=layer))
    return "REINSTATED"

```

## `src/c4_exec/overnight.py`

```python
"""D1 overnight-hold rule (phase4-design-v1_0).

Deterministic C4 rule at 15:45 ET for the SHORT lane, one journaled
OVERNIGHT_HOLD_DECISION per open position, evaluated in this order:

  1. earnings next session (when known)      -> EXIT (gap risk trumps all)
  2. unrealized >= +0.3R                     -> HOLD (winners get the night)
  3. age < 1 session AND realized_fraction
     of predicted move < 0.5                 -> HOLD (thesis needs time)
  4. otherwise                               -> EXIT (stale flat positions
                                                don't earn overnight risk)

LONG lane: default HOLD, no decision rows (holding IS the strategy).
Exits are limit-at-bid; unfilled orders reprice at 15:55 (one step). Still
unfilled at close -> the DAY order expires, the position holds overnight
with its catastrophe stop intact, journaled as OVERNIGHT_FORCED_HOLD.
The pure decision function is separable for the test matrix.
"""
from __future__ import annotations

from typing import Optional


def overnight_decision(unrealized_r: float, session_age: int,
                       realized_fraction: float,
                       earnings_next_session: Optional[bool],
                       cfg: dict) -> tuple[str, str]:
    """(HOLD|EXIT, rule_tag). realized_fraction = fraction of the predicted
    move achieved at the mark; earnings_next_session None = unknown (D7)."""
    if earnings_next_session:
        return "EXIT", "earnings_next_session"
    if unrealized_r >= float(cfg["hold_min_unrealized_R"]):
        return "HOLD", "unrealized_R_threshold"
    if session_age < int(cfg["young_max_age_sessions"]) and \
            realized_fraction < float(cfg["young_max_realized_fraction"]):
        return "HOLD", "young_position"
    return "EXIT", "stale_flat"


def realized_move_fraction(mark: float, avg_entry: float,
                           magnitude_est: float) -> float:
    if magnitude_est <= 0:
        return 0.0
    return ((mark - avg_entry) / avg_entry) / magnitude_est

```

## `src/c4_exec/reconcile.py`

```python
"""C4 startup + periodic reconciliation (baseline v0.4/v0.5).

The broker is the source of truth, ALWAYS. On boot — BEFORE any intent is
accepted — and every reconcile_interval_min thereafter:

  1. Pull broker account, positions, open orders.
  2. Local OPEN position missing at broker  -> mark CLOSED_EXTERNAL
     (status CLOSED, RECONCILED event, audit row, alert health).
  3. Broker position missing locally        -> ADOPTED skeleton position row
     (no thesis lineage — operator review; audit + alert).
  4. Quantity drift                          -> local qty snapped to broker,
     RECONCILED event with old/new.
  5. Refresh capital rows in journal.control: broker_equity, settled_cash,
     last_reconcile_ts. Effective capital = min(broker_equity,
     trading_capital) is DERIVED by readers (A3, pre-flight) — never stored,
     never stale relative to an operator capital change.
"""
from __future__ import annotations

from common.broker import Broker
from common.clock import utcnow
from common.db import get_pool, jb
from common.journal import active_config_version
from common.log import get_logger, kv
from c1_ingestion.heartbeat import set_health

from .flags import set_flag
from .state import position_event

log = get_logger("c4.reconcile")


async def reconcile(broker: Broker) -> dict:
    account = await broker.get_account()
    broker_positions = {p.ticker: p for p in await broker.get_positions()}

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT position_id, ticker, qty_open, avg_entry
               FROM journal.positions WHERE status='OPEN'""")
        local = await cur.fetchall()

    summary = {"closed_external": [], "adopted": [], "qty_snapped": [],
               "equity": account.equity, "settled_cash": account.settled_cash}
    seen = set()

    async with pool.connection() as conn:
        async with conn.transaction():
            for position_id, ticker, qty_open, avg_entry in local:
                seen.add(ticker)
                bp = broker_positions.get(ticker)
                if bp is None or bp.qty <= 0:
                    await conn.execute(
                        """UPDATE journal.positions
                           SET status='CLOSED', closed_ts=now(), qty_open=0
                           WHERE position_id=%s""", (position_id,))
                    await position_event(position_id, "RECONCILED", "BROKER",
                                         old_value={"qty_open": qty_open},
                                         new_value={"qty_open": 0},
                                         detail="CLOSED_EXTERNAL: missing at broker",
                                         conn=conn)
                    await conn.execute(
                        """INSERT INTO journal.audit (actor, action, old_value,
                             new_value, detail)
                           VALUES ('C4','RECONCILE_CLOSED_EXTERNAL',%s,%s,%s)""",
                        (str(qty_open), "0", ticker))
                    summary["closed_external"].append(ticker)
                elif bp.qty != qty_open:
                    await conn.execute(
                        """UPDATE journal.positions SET qty_open=%s
                           WHERE position_id=%s""", (bp.qty, position_id))
                    await position_event(position_id, "RECONCILED", "BROKER",
                                         old_value={"qty_open": qty_open},
                                         new_value={"qty_open": bp.qty},
                                         detail="qty snapped to broker", conn=conn)
                    summary["qty_snapped"].append(ticker)

            for ticker, bp in broker_positions.items():
                if ticker in seen or bp.qty <= 0:
                    continue
                # ADOPTED skeleton: no thesis lineage; conservative synthetic
                # policy (operator must review). r_unit from a 2% notional stop.
                stop = round(bp.avg_entry * 0.98, 2)
                cur = await conn.execute(
                    """INSERT INTO journal.decisions
                       (signal_id, stage, agent, action, ticker, reason,
                        payload, config_version)
                       VALUES (%s,'ORDER','C4','ADOPTED',%s,
                               'position found at broker with no local record',
                               %s,%s)
                       RETURNING decision_id""",
                    (f"adopted:{ticker}:{utcnow().date()}", ticker,
                     jb({"qty": bp.qty, "avg_entry": bp.avg_entry}),
                     active_config_version()))
                dec_id = (await cur.fetchone())[0]
                cur = await conn.execute(
                    """INSERT INTO journal.intents
                       (intent_id, decision_id, ticker, side, qty, limit_price,
                        status, config_version)
                       VALUES (%s,%s,%s,'BUY',%s,%s,'FILLED',%s)
                       ON CONFLICT (intent_id) DO NOTHING""",
                    (f"adopted-{ticker}-{utcnow().date()}", dec_id, ticker,
                     bp.qty, bp.avg_entry, active_config_version()))
                cur = await conn.execute(
                    """INSERT INTO journal.positions
                       (ticker, horizon, profile, status, opened_ts,
                        entry_intent_id, thesis_decision_id, qty_initial,
                        qty_open, avg_entry, initial_stop, r_unit, exit_policy,
                        config_version)
                       VALUES (%s,'SHORT','adopted_v1','OPEN',now(),%s,%s,%s,
                               %s,%s,%s,%s,%s,%s)
                       RETURNING position_id""",
                    (ticker, f"adopted-{ticker}-{utcnow().date()}", dec_id,
                     bp.qty, bp.qty, bp.avg_entry, stop,
                     round(bp.avg_entry - stop, 4),
                     jb({"profile": "adopted_v1",
                         "initial_stop": {"method": "pct", "price": stop},
                         "note": "ADOPTED at reconciliation — operator review"}),
                     active_config_version()))
                pid = (await cur.fetchone())[0]
                await position_event(pid, "RECONCILED", "BROKER",
                                     new_value={"qty": bp.qty,
                                                "avg_entry": bp.avg_entry},
                                     detail="ADOPTED: broker position with no local record",
                                     conn=conn)
                await conn.execute(
                    """INSERT INTO journal.audit (actor, action, new_value, detail)
                       VALUES ('C4','RECONCILE_ADOPTED',%s,%s)""",
                    (str(bp.qty), ticker))
                summary["adopted"].append(ticker)

    await set_flag("broker_equity", f"{account.equity:.2f}", "C4",
                   "reconciliation refresh")
    await set_flag("settled_cash", f"{account.settled_cash:.2f}", "C4",
                   "reconciliation refresh")
    await set_flag("last_reconcile_ts", utcnow().isoformat(), "C4")

    status = "OK"
    detail = f"equity={account.equity:.0f}"
    if summary["closed_external"] or summary["adopted"]:
        status = "DEGRADED"
        detail = (f"drift: closed_external={summary['closed_external']} "
                  f"adopted={summary['adopted']}")
    await set_health("broker_api", status, detail)
    log.info("reconciled", extra=kv(**{k: v for k, v in summary.items()
                                       if k in ("equity", "closed_external",
                                                "adopted", "qty_snapped")}))
    return summary

```

## `src/c4_exec/service.py`

```python
"""C4 Execution service — Phase 4 chunk 1: reconciliation gate + entry flow.
(Exit engine, overnight rule, and dead-man monitors land in chunk 2; the
module boundaries here are built for them.)

Entry flow per intent on exec.intent:
  1. idempotency: intent already SUBMITTED/FILLED or an ENTRY order exists
     for it -> no-op ack (crash-replay safe at BOTH layers: intent_id here,
     client_order_id at the broker).
  2. pre-flight (deployed notional + new <= effective capital; settled cash;
     kill/breaker/BLOCK_ENTRIES; halt) — a second, independent enforcement
     of what A3 already checked, because A3's read and C4's submit are
     different moments.
  3. submit limit DAY, client_order_id = intent_id.
  4. poll to terminal (entry orders are DAY-limited; polling cadence 1s in
     tests via injectable sleep, 2s live).
  5. on fill: ONE TRANSACTION — position row + STOPS_PLACED event + EXEC
     decision; then place the broker-resident catastrophe stop (stop-market
     GTC at the policy's materialized price, never moved) and link it.
Partial fills at day end: position sized to filled qty, remainder expires.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal as _signal
from typing import Optional

from common.broker import BrokerReject, get_broker
from common.clock import utcnow
from common.config import config_path, load_yaml
from common.db import get_pool, close_pool
from common.journal import (active_config_version, register_config_version,
                            write_decision)
from common.log import get_logger, kv
from common.queue import ack, claim, enqueue, fail, wait_for_message
from c1_ingestion.heartbeat import set_health

from .flags import ensure_defaults, entries_blocked, get_flag
from .reconcile import reconcile
from .state import (create_order, create_position, open_positions,
                    position_event, record_fill, transition_order)

log = get_logger("c4.service")

IN_QUEUE = "exec.intent"
CONSUMER = f"c4-{os.getpid()}"


class C4Service:
    def __init__(self, cfg: dict, broker=None, now_fn=None,
                 poll_sleep: float = 2.0, fill_timeout_secs: float = 300.0):
        self.cfg = cfg["c4"]
        self.broker = broker or get_broker()
        self.now_fn = now_fn or utcnow
        self.poll_sleep = poll_sleep
        self.fill_timeout_secs = fill_timeout_secs

    # ---------------------------------------------------------------- entries
    async def _already_processed(self, intent_id: str) -> bool:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT status FROM journal.intents WHERE intent_id=%s""",
                (intent_id,))
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"intent not in journal: {intent_id}")
            if row[0] in ("SUBMITTED", "FILLED", "PARTIAL", "CANCELLED",
                          "EXPIRED", "REJECTED"):
                return True
            cur = await conn.execute(
                """SELECT count(*) FROM journal.orders
                   WHERE intent_id=%s AND order_role='ENTRY'""", (intent_id,))
            return (await cur.fetchone())[0] > 0

    async def _preflight(self, body: dict) -> Optional[str]:
        if await entries_blocked():
            return "ENTRIES_BLOCKED"
        equity = float(await get_flag("broker_equity", "0") or 0)
        capital = float(await get_flag("trading_capital", "0") or 0)
        effective = min(equity, capital)
        settled = float(await get_flag("settled_cash", "0") or 0)
        notional = body["qty"] * body["limit_price"]
        deployed = sum(p["qty_open"] * float(p["avg_entry"])
                       for p in await open_positions())
        if deployed + notional > effective:
            return "CAPITAL_PREFLIGHT"
        if notional > settled:
            return "SETTLED_CASH"
        return None

    async def _set_intent_status(self, intent_id: str, status: str,
                                 conn=None) -> None:
        async def _run(c):
            await c.execute(
                "UPDATE journal.intents SET status=%s WHERE intent_id=%s",
                (status, intent_id))
        if conn is not None:
            await _run(conn)
        else:
            pool = await get_pool()
            async with pool.connection() as c:
                await _run(c)

    async def handle_intent(self, msg) -> None:
        body = msg.payload.get("body") or {}
        trace = msg.payload.get("envelope", {}).get("trace", {})
        intent_id = body.get("intent_id")
        if not intent_id or not body.get("ticker") or not body.get("qty"):
            raise ValueError(f"malformed intent message ({msg.dedup_key})")

        if await self._already_processed(intent_id):
            log.info("intent no-op (idempotent)", extra=kv(intent_id=intent_id))
            return

        veto = await self._preflight(body)
        if veto:
            await self._set_intent_status(intent_id, "REJECTED")
            await write_decision(
                signal_id=trace.get("signal_id") or intent_id,
                item_id=trace.get("item_id"), ticker=body["ticker"],
                stage="ORDER", agent="C4", action="VETO", veto_reason=veto,
                payload={"intent_id": intent_id, "qty": body["qty"],
                         "limit_price": body["limit_price"]},
                reason=f"pre-flight {veto}")
            log.warning("pre-flight veto", extra=kv(intent_id=intent_id,
                                                    reason=veto))
            return

        try:
            border = await self.broker.submit_limit(
                body["ticker"], "BUY", int(body["qty"]),
                float(body["limit_price"]), client_order_id=intent_id,
                tif="day")
        except BrokerReject as e:
            await self._set_intent_status(intent_id, "REJECTED")
            await write_decision(
                signal_id=trace.get("signal_id") or intent_id,
                item_id=trace.get("item_id"), ticker=body["ticker"],
                stage="ORDER", agent="C4", action="VETO",
                veto_reason="BROKER_REJECT",
                payload={"intent_id": intent_id, "error": str(e)[:300]},
                reason="broker rejected entry")
            return

        order_id = await create_order(intent_id, "ENTRY", border)
        await self._set_intent_status(intent_id, "SUBMITTED")
        log.info("entry submitted", extra=kv(intent_id=intent_id,
                                             broker_order_id=border.broker_order_id))

        border = await self._await_terminal(border.broker_order_id)
        await transition_order(order_id, border)

        if border.filled_qty <= 0:
            await self._set_intent_status(
                intent_id, "EXPIRED" if border.status == "expired" else "CANCELLED")
            log.info("entry unfilled", extra=kv(intent_id=intent_id,
                                                status=border.status))
            return

        fill_price = float(border.filled_avg_price)
        await record_fill(order_id, self.now_fn(), border.filled_qty,
                          fill_price, f"{border.broker_order_id}-fill")
        await self._set_intent_status(
            intent_id, "FILLED" if border.filled_qty >= border.qty else "PARTIAL")
        await self._open_position(body, trace, intent_id, order_id,
                                  border.filled_qty, fill_price)

    async def _await_terminal(self, broker_order_id: str):
        waited = 0.0
        while True:
            o = await self.broker.get_order(broker_order_id)
            if o.terminal:
                return o
            if waited >= self.fill_timeout_secs:
                await self.broker.cancel(broker_order_id)
                return await self.broker.get_order(broker_order_id)
            await asyncio.sleep(self.poll_sleep)
            waited += self.poll_sleep

    async def _open_position(self, body: dict, trace: dict, intent_id: str,
                             entry_order_id: int, qty: int,
                             fill_price: float) -> None:
        policy = dict(body["exit_policy"])
        atr = float(policy["atr_14"])
        # re-materialize stops off the ACTUAL fill (A3 anticipated the limit)
        k = float(policy["initial_stop"]["k"])
        cat_k = float(policy["catastrophe_stop_broker"]["k"])
        policy["initial_stop"]["price"] = round(fill_price - k * atr, 2)
        policy["catastrophe_stop_broker"]["price"] = round(
            fill_price - cat_k * atr, 2)

        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                position_id = await create_position(
                    ticker=body["ticker"], horizon=body["horizon"],
                    profile=policy["profile"], entry_intent_id=intent_id,
                    thesis_decision_id=body["thesis_decision_id"],
                    item_id=trace.get("item_id"), qty=qty,
                    avg_entry=fill_price,
                    initial_stop=policy["initial_stop"]["price"],
                    exit_policy=policy,
                    config_version=active_config_version(),
                    opened_ts=self.now_fn(), conn=conn)
                await conn.execute(
                    """UPDATE journal.orders SET position_id=%s
                       WHERE order_id=%s""", (position_id, entry_order_id))
                await write_decision(
                    signal_id=trace.get("signal_id") or intent_id,
                    item_id=trace.get("item_id"), ticker=body["ticker"],
                    stage="ORDER", agent="C4", action="FILLED",
                    payload={"intent_id": intent_id, "position_id": position_id,
                             "qty": qty, "fill_price": fill_price,
                             "exit_policy": policy},
                    reason=f"entry filled {qty} @ {fill_price}", conn=conn)

        # catastrophe stop OUTSIDE the tx: a fill without its stop must be
        # retried, not rolled back (the position exists at the broker either way)
        cat_price = policy["catastrophe_stop_broker"]["price"]
        try:
            stop_order = await self.broker.submit_stop(
                body["ticker"], "SELL", qty, cat_price,
                client_order_id=f"cat-{intent_id}")
            stop_row_id = await create_order(None, "CATASTROPHE_STOP",
                                             stop_order,
                                             position_id=position_id)
            async with pool.connection() as conn:
                await conn.execute(
                    """UPDATE journal.positions
                       SET catastrophe_stop_order_id=%s WHERE position_id=%s""",
                    (stop_row_id, position_id))
            await position_event(position_id, "STOPS_PLACED", "C4",
                                 new_value={"initial_stop": policy["initial_stop"],
                                            "catastrophe": {
                                                "price": cat_price,
                                                "broker_order_id":
                                                    stop_order.broker_order_id}},
                                 detail="two-tier stops armed")
        except Exception as e:
            # position exists but is protected only by synthetic layers: alarm
            await set_health("exec", "DEGRADED",
                             f"CATASTROPHE STOP FAILED {body['ticker']}: {repr(e)[:150]}")
            await position_event(position_id, "STOPS_PLACED", "C4",
                                 new_value={"initial_stop": policy["initial_stop"],
                                            "catastrophe": None},
                                 detail=f"CATASTROPHE FAILED: {repr(e)[:150]}")
            log.error("catastrophe stop placement failed",
                      extra=kv(position_id=position_id, error=repr(e)[:200]))
            return
        log.info("position opened", extra=kv(position_id=position_id,
                                             ticker=body["ticker"], qty=qty,
                                             fill=fill_price, cat=cat_price))


async def consume_loop(svc: C4Service, stop: asyncio.Event) -> None:
    await set_health("exec", "OK", f"consuming {IN_QUEUE}")
    while not stop.is_set():
        msg = await claim(IN_QUEUE, CONSUMER)
        if msg is None:
            try:
                await asyncio.wait_for(wait_for_message(IN_QUEUE, timeout_secs=5.0), 6.0)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await svc.handle_intent(msg)
            await ack(msg.msg_id)
        except Exception as e:
            log.error("intent failed", extra=kv(msg_id=msg.msg_id,
                                                error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def engine_loop(svc: C4Service, engine, marketdata, stop: asyncio.Event,
                      deadman_cfg: dict, exit_cfg: dict,
                      interval_secs: float = 60.0) -> None:
    """Per-minute during RTH: bars -> halt check -> engine.step per open
    position; overnight passes at 15:45/15:55 ET; dead-man + breaker every
    pass. Exit engine suspends (catastrophe stops sole protection) when the
    dead-man says marketdata is too stale to trust synthetic layers."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from a3_risk.service import minutes_to_close
    from .breaker import check_breaker
    from .deadman import check as deadman_check
    from .flags import get_flag
    from .state import open_positions

    ET = ZoneInfo("America/New_York")
    overnight_done: dict[str, str] = {}          # date -> last pass label

    while not stop.is_set():
        now = svc.now_fn()
        in_session = minutes_to_close(now) is not None
        try:
            await deadman_check(deadman_cfg["components"] and deadman_cfg,
                                now, in_session)
            await check_breaker(float(svc.cfg["drawdown_breaker_pct"]))
            await set_health("exec", "OK", "engine loop")

            if in_session and (await get_flag("exit_engine_suspended")) != "1":
                for pos in await open_positions():
                    if await engine.check_halt(pos):
                        continue
                    end = now
                    start = end - timedelta(minutes=3)
                    bars = await marketdata.minute_bars(pos["ticker"], start, end)
                    if not bars:
                        continue                  # halt heuristic accumulates
                    b = bars[-1]
                    await engine.step(pos, b)

                et = now.astimezone(ET)
                today = et.date().isoformat()
                hhmm = et.strftime("%H:%M")
                oc = exit_cfg["overnight_rule"]
                if hhmm >= "15:55" and overnight_done.get(today) == "15:45":
                    await engine.overnight_pass(oc, pass_label="15:55")
                    overnight_done[today] = "15:55"
                elif hhmm >= oc.get("check_time_et", "15:45") \
                        and today not in overnight_done:
                    await engine.overnight_pass(oc, pass_label="15:45")
                    overnight_done[today] = "15:45"
            else:
                # after the close, once: session-tf MIP predicates evaluate
                # on the finished session bar
                et = now.astimezone(ET)
                today = et.date().isoformat()
                if et.strftime("%H:%M") >= "16:01" \
                        and overnight_done.get(today) != "session_close":
                    async def _daily(ticker):
                        bars = await marketdata.daily_bars(ticker, 1)
                        return bars[-1] if bars else None
                    await engine.session_close_pass(_daily)
                    overnight_done[today] = "session_close"
        except Exception as e:
            log.error("engine loop error", extra=kv(error=repr(e)[:300]))
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_secs)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    from common.marketdata import get_marketdata
    from .engine import PositionEngine

    cfg = load_yaml(config_path("deadman.yaml"))
    exit_cfg = load_yaml(config_path("exit_profiles.yaml"))
    await register_config_version("c4 exec service startup")
    await ensure_defaults()
    svc = C4Service(cfg)
    engine = PositionEngine(
        svc.broker, now_fn=svc.now_fn,
        unprotected_max_secs=float(cfg["c4"]["exit_unprotected_max_secs"]))
    marketdata = get_marketdata()
    # reconciliation gate: NO intents accepted before this completes
    await reconcile(svc.broker)
    log.info("C4 up (reconciled)", extra=kv(consumer=CONSUMER))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def periodic_reconcile():
        interval = float(svc.cfg.get("reconcile_interval_min", 15)) * 60
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                try:
                    await reconcile(svc.broker)
                except Exception as e:
                    log.error("periodic reconcile failed",
                              extra=kv(error=repr(e)[:200]))
                    await set_health("broker_api", "DEGRADED", repr(e)[:200])

    recon_task = asyncio.create_task(periodic_reconcile())
    eng_task = asyncio.create_task(
        engine_loop(svc, engine, marketdata, stop, cfg, exit_cfg))
    await consume_loop(svc, stop)
    recon_task.cancel()
    eng_task.cancel()
    await set_health("exec", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

```

## `src/c4_exec/state.py`

```python
"""C4 persistence — the order state machine's DB operations. Intents table is
the authority; orders/fills/positions/position_events/exits record every
transition (journal schema v1, populated from Phase 4 on).

State maps: broker status -> orders.state
  accepted -> ACCEPTED, partially_filled -> PARTIAL, filled -> FILLED,
  canceled -> CANCELLED, rejected -> REJECTED, expired -> EXPIRED.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from common.broker import BrokerOrder
from common.db import get_pool, jb
from common.log import get_logger, kv

log = get_logger("c4.state")

BROKER_STATE = {"accepted": "ACCEPTED", "new": "ACCEPTED",
                "partially_filled": "PARTIAL", "filled": "FILLED",
                "canceled": "CANCELLED", "rejected": "REJECTED",
                "expired": "EXPIRED"}


def order_state(o: BrokerOrder) -> str:
    return BROKER_STATE.get(o.status, "ACCEPTED")


async def create_order(intent_id: Optional[str], role: str, o: BrokerOrder,
                       position_id: Optional[int] = None, conn=None) -> int:
    async def _run(c):
        cur = await c.execute(
            """INSERT INTO journal.orders
               (intent_id, position_id, broker_order_id, order_role, state,
                qty, limit_price, stop_price, submitted_ts, raw)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING order_id""",
            (intent_id, position_id, o.broker_order_id, role, order_state(o),
             o.qty, o.limit_price, o.stop_price, o.submitted_ts, jb(o.raw)))
        return (await cur.fetchone())[0]
    if conn is not None:
        return await _run(conn)
    pool = await get_pool()
    async with pool.connection() as c:
        return await _run(c)


async def transition_order(order_id: int, o: BrokerOrder, conn=None) -> None:
    async def _run(c):
        state = order_state(o)
        await c.execute(
            """UPDATE journal.orders SET state=%s, raw=%s,
                      closed_ts = CASE WHEN %s IN
                        ('FILLED','CANCELLED','REJECTED','EXPIRED')
                        THEN now() ELSE closed_ts END
               WHERE order_id=%s""",
            (state, jb(o.raw), state, order_id))
    if conn is not None:
        await _run(conn)
    else:
        pool = await get_pool()
        async with pool.connection() as c:
            await _run(c)


async def record_fill(order_id: int, ts: datetime, qty: int, price: float,
                      broker_exec_id: str, conn=None) -> None:
    async def _run(c):
        await c.execute(
            """INSERT INTO journal.fills (order_id, ts, qty, price, broker_exec_id)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (broker_exec_id) DO NOTHING""",
            (order_id, ts, qty, price, broker_exec_id))
    if conn is not None:
        await _run(conn)
    else:
        pool = await get_pool()
        async with pool.connection() as c:
            await _run(c)


async def create_position(ticker: str, horizon: str, profile: str,
                          entry_intent_id: str, thesis_decision_id: int,
                          item_id: Optional[str], qty: int, avg_entry: float,
                          initial_stop: float, exit_policy: dict,
                          config_version: str, opened_ts: datetime,
                          conn=None) -> int:
    r_unit = round(avg_entry - initial_stop, 4)
    async def _run(c):
        cur = await c.execute(
            """INSERT INTO journal.positions
               (ticker, horizon, profile, status, opened_ts, entry_intent_id,
                thesis_decision_id, item_id, qty_initial, qty_open, avg_entry,
                initial_stop, r_unit, exit_policy, config_version)
               VALUES (%s,%s,%s,'OPEN',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING position_id""",
            (ticker, horizon, profile, opened_ts, entry_intent_id,
             thesis_decision_id, item_id, qty, qty, avg_entry, initial_stop,
             r_unit, jb(exit_policy), config_version))
        return (await cur.fetchone())[0]
    if conn is not None:
        return await _run(conn)
    pool = await get_pool()
    async with pool.connection() as c:
        return await _run(c)


async def position_event(position_id: int, event_type: str, actor: str,
                         old_value=None, new_value=None,
                         r_progress: Optional[float] = None,
                         detail: str = "", decision_id: Optional[int] = None,
                         conn=None) -> None:
    async def _run(c):
        await c.execute(
            """INSERT INTO journal.position_events
               (position_id, event_type, actor, old_value, new_value,
                r_progress, detail, decision_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (position_id, event_type, actor,
             jb(old_value) if old_value is not None else None,
             jb(new_value) if new_value is not None else None,
             r_progress, detail[:300], decision_id))
    if conn is not None:
        await _run(conn)
    else:
        pool = await get_pool()
        async with pool.connection() as c:
            await _run(c)


async def record_exit(position_id: int, order_id: Optional[int], ts: datetime,
                      exit_layer: str, qty: int, price: float,
                      avg_entry: float, r_unit: float, is_partial: bool,
                      conn=None) -> None:
    pnl = round((price - avg_entry) * qty, 4)
    r_multiple = round(pnl / (r_unit * qty), 3) if r_unit else 0.0
    async def _run(c):
        await c.execute(
            """INSERT INTO journal.exits
               (position_id, order_id, ts, exit_layer, qty, price,
                realized_pnl, r_multiple, is_partial)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (position_id, order_id, ts, exit_layer, qty, price, pnl,
             r_multiple, is_partial))
        await c.execute(
            """UPDATE journal.positions
               SET qty_open = qty_open - %s,
                   realized_pnl = realized_pnl + %s,
                   status = CASE WHEN qty_open - %s <= 0 THEN 'CLOSED'
                                 ELSE status END,
                   closed_ts = CASE WHEN qty_open - %s <= 0 THEN now()
                                    ELSE closed_ts END
               WHERE position_id=%s""",
            (qty, pnl, qty, qty, position_id))
        await position_event(position_id, "EXIT" if not is_partial else "SCALE_OUT",
                             "C4", new_value={"layer": exit_layer, "qty": qty,
                                              "price": price, "pnl": pnl},
                             detail=exit_layer, conn=c)
    if conn is not None:
        await _run(conn)
    else:
        pool = await get_pool()
        async with pool.connection() as c:
            await _run(c)


async def open_positions() -> list[dict]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT position_id, ticker, horizon, profile, qty_open,
                      avg_entry, initial_stop, r_unit, exit_policy,
                      catastrophe_stop_order_id, opened_ts, realized_pnl,
                      last_price
               FROM journal.positions WHERE status='OPEN'""")
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in await cur.fetchall()]

```

## `src/c8_regime/__init__.py`

```python

```

## `src/c8_regime/service.py`

```python
"""C8 Regime Context Builder (code, no model).

Phase 3 decision: features from ETF proxies on the same Alpaca data — no VIX
feed exists on the free tier, so the volatility field is honest-named
`realized_vol_20d` (SPY close-to-close, annualized), NOT a fake `vix`.
Swap in real VIX when a provider arrives; consumers key on field names.

Features written to journal.regime_snapshots.features:
  index_trend        "above_50d" | "below_50d"
  index_trend_slope  20d SMA-of-50d-SMA slope sign: "rising" | "falling" | "flat"
  realized_vol_20d   annualized SPY realized vol (VIX proxy)
  breadth_proxy      fraction of the 11 SPDR sector ETFs above their own 50d
  sector_rs          top/bottom 3 sectors by 20d relative strength vs SPY

Service: writes a snapshot every interval (config/c8.yaml); every A2 decision
references the latest regime_id.
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal

from common.clock import utcnow
from common.config import config_path, load_yaml
from common.db import close_pool, get_pool, jb
from common.log import get_logger, kv
from common.marketdata import MarketData, get_marketdata, realized_vol, sma

log = get_logger("c8.regime")

SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE",
               "XLI", "XLB", "XLU", "XLRE", "XLC"]
INDEX = "SPY"


async def compute_features(md: MarketData) -> dict:
    spy = await md.daily_bars(INDEX, 80)
    closes = [b["close"] for b in spy]
    sma50 = sma(closes, 50)
    last = closes[-1] if closes else None

    features: dict = {}
    if last is not None and sma50 is not None:
        features["index_trend"] = "above_50d" if last >= sma50 else "below_50d"
        sma50_prev = sma(closes[:-20], 50)
        if sma50_prev:
            delta = (sma50 - sma50_prev) / sma50_prev
            features["index_trend_slope"] = ("rising" if delta > 0.002 else
                                             "falling" if delta < -0.002 else "flat")
    rv = realized_vol(spy, 20)
    if rv is not None:
        features["realized_vol_20d"] = rv

    above = 0, 0
    counted, above_n = 0, 0
    rs: dict[str, float] = {}
    spy_ret20 = (closes[-1] / closes[-21] - 1) if len(closes) >= 21 else None
    for etf in SECTOR_ETFS:
        bars = await md.daily_bars(etf, 80)
        c = [b["close"] for b in bars]
        s50 = sma(c, 50)
        if s50 is not None and c:
            counted += 1
            if c[-1] >= s50:
                above_n += 1
        if spy_ret20 is not None and len(c) >= 21:
            rs[etf] = round((c[-1] / c[-21] - 1) - spy_ret20, 4)
    if counted:
        features["breadth_proxy"] = round(above_n / counted, 3)
    if rs:
        ranked = sorted(rs.items(), key=lambda kv_: kv_[1], reverse=True)
        features["sector_rs"] = {"top": dict(ranked[:3]), "bottom": dict(ranked[-3:])}

    features["computed_ts"] = utcnow().isoformat()
    features["source"] = "etf_proxies_iex"        # provenance: not a real VIX/breadth feed
    return features


async def write_snapshot(md: MarketData) -> int:
    features = await compute_features(md)
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO journal.regime_snapshots (ts, features)
               VALUES (now(), %s) RETURNING regime_id""",
            (jb(features),))
        regime_id = (await cur.fetchone())[0]
    log.info("regime snapshot", extra=kv(regime_id=regime_id,
                                         trend=features.get("index_trend"),
                                         rv=features.get("realized_vol_20d")))
    return regime_id


async def latest_regime_id() -> int | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT regime_id FROM journal.regime_snapshots ORDER BY ts DESC LIMIT 1")
        row = await cur.fetchone()
        return row[0] if row else None


async def main() -> None:
    cfg = load_yaml(config_path("c8.yaml"))
    md = get_marketdata()
    interval_open = float(cfg.get("interval_market_secs", 1800))
    interval_closed = float(cfg.get("interval_offhours_secs", 3600))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    from router.facts import market_open_now
    from c1_ingestion.heartbeat import set_health
    await set_health("regime", "OK", "snapshot loop running")
    while not stop.is_set():
        try:
            await write_snapshot(md)
        except Exception as e:
            log.error("snapshot failed", extra=kv(error=repr(e)[:200]))
            await set_health("regime", "DEGRADED", repr(e)[:200])
        wait = interval_open if market_open_now() else interval_closed
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    await set_health("regime", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

```

## `src/common/__init__.py`

```python

```

## `src/common/broker.py`

```python
"""Broker layer (Phase 4). Alpaca paper first (design D7 confirmation);
IBKR remains the fill-quality upgrade path behind the same protocol.

LLMs never touch this module (baseline rule: LLMs never call the broker API).
Only C4 submits/cancels; A3 reads capital numbers from C4's reconciliation
rows in journal.control, never from here.

Providers:
  AlpacaBroker — httpx against https://paper-api.alpaca.markets (env
                 ALPACA_KEY_ID / ALPACA_SECRET_KEY; PAPER endpoint is
                 hard-coded until real capital is a decision).
  FakeBroker   — programmable fixture broker: scripted fill behaviors per
                 client_order_id or ticker (fill / partial / reject / rest),
                 mutating account + positions state, drift injection for
                 reconciliation tests.

All prices float, all qty int, all timestamps aware UTC.
"""
from __future__ import annotations

import itertools
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol

import httpx

from .clock import utcnow
from .log import get_logger

log = get_logger("broker")


@dataclass
class Account:
    equity: float
    settled_cash: float          # cash-account rule: buying power = settled cash
    currency: str = "USD"


@dataclass
class BrokerPosition:
    ticker: str
    qty: int
    avg_entry: float


@dataclass
class BrokerOrder:
    broker_order_id: str
    client_order_id: Optional[str]
    ticker: str
    side: str                    # BUY | SELL
    order_type: str              # limit | stop
    qty: int
    limit_price: Optional[float]
    stop_price: Optional[float]
    status: str                  # accepted | partially_filled | filled |
                                 # canceled | rejected | expired
    filled_qty: int = 0
    filled_avg_price: Optional[float] = None
    submitted_ts: Optional[datetime] = None
    raw: dict = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in ("filled", "canceled", "rejected", "expired")


class Broker(Protocol):
    async def get_account(self) -> Account: ...
    async def get_positions(self) -> list[BrokerPosition]: ...
    async def get_open_orders(self) -> list[BrokerOrder]: ...
    async def get_order(self, broker_order_id: str) -> BrokerOrder: ...
    async def submit_limit(self, ticker: str, side: str, qty: int,
                           limit_price: float, client_order_id: str,
                           tif: str = "day") -> BrokerOrder: ...
    async def submit_stop(self, ticker: str, side: str, qty: int,
                          stop_price: float, client_order_id: str,
                          tif: str = "gtc") -> BrokerOrder: ...
    async def cancel(self, broker_order_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# Alpaca (paper)
# ---------------------------------------------------------------------------

class AlpacaBroker:
    BASE = "https://paper-api.alpaca.markets"     # paper hard-coded (Phase 4)

    def __init__(self):
        key = os.environ.get("ALPACA_KEY_ID")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    async def _req(self, method: str, path: str, json: dict | None = None) -> dict | list:
        async with httpx.AsyncClient(timeout=15.0, headers=self._headers) as c:
            resp = await c.request(method, f"{self.BASE}{path}", json=json)
            if resp.status_code == 422 and method == "POST":
                raise BrokerReject(resp.json().get("message", "rejected"))
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    @staticmethod
    def _order(o: dict) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=o["id"], client_order_id=o.get("client_order_id"),
            ticker=o["symbol"], side=o["side"].upper(),
            order_type=o["type"], qty=int(float(o["qty"])),
            limit_price=float(o["limit_price"]) if o.get("limit_price") else None,
            stop_price=float(o["stop_price"]) if o.get("stop_price") else None,
            status=o["status"], filled_qty=int(float(o.get("filled_qty") or 0)),
            filled_avg_price=(float(o["filled_avg_price"])
                              if o.get("filled_avg_price") else None),
            submitted_ts=(datetime.fromisoformat(o["submitted_at"].replace("Z", "+00:00"))
                          if o.get("submitted_at") else None),
            raw=o)

    async def get_account(self) -> Account:
        a = await self._req("GET", "/v2/account")
        # cash-account settled funds: Alpaca exposes non_marginable_buying_power
        settled = float(a.get("non_marginable_buying_power") or a.get("cash") or 0)
        return Account(equity=float(a["equity"]), settled_cash=settled)

    async def get_positions(self) -> list[BrokerPosition]:
        rows = await self._req("GET", "/v2/positions")
        return [BrokerPosition(p["symbol"], int(float(p["qty"])),
                               float(p["avg_entry_price"])) for p in rows]

    async def get_open_orders(self) -> list[BrokerOrder]:
        rows = await self._req("GET", "/v2/orders?status=open&limit=500")
        return [self._order(o) for o in rows]

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        return self._order(await self._req("GET", f"/v2/orders/{broker_order_id}"))

    async def submit_limit(self, ticker, side, qty, limit_price,
                           client_order_id, tif="day") -> BrokerOrder:
        return self._order(await self._req("POST", "/v2/orders", {
            "symbol": ticker, "side": side.lower(), "type": "limit",
            "qty": str(qty), "limit_price": f"{limit_price:.2f}",
            "time_in_force": tif, "client_order_id": client_order_id}))

    async def submit_stop(self, ticker, side, qty, stop_price,
                          client_order_id, tif="gtc") -> BrokerOrder:
        return self._order(await self._req("POST", "/v2/orders", {
            "symbol": ticker, "side": side.lower(), "type": "stop",
            "qty": str(qty), "stop_price": f"{stop_price:.2f}",
            "time_in_force": tif, "client_order_id": client_order_id}))

    async def cancel(self, broker_order_id: str) -> bool:
        try:
            await self._req("DELETE", f"/v2/orders/{broker_order_id}")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False       # already terminal
            raise


class BrokerReject(Exception):
    pass


# ---------------------------------------------------------------------------
# Fake (tests/dev)
# ---------------------------------------------------------------------------

@dataclass
class FakeBroker:
    """Programmable broker. Behaviors are scripted per client_order_id (exact)
    or per ticker (fallback); default behavior is immediate full fill for
    limits and resting acceptance for stops.

    behavior values:
      "fill"           accept + immediately fill at limit/stop price
      "partial:<n>"    accept + fill n shares, remain partially_filled
      "rest"           accept, never fill (until fill_order() called)
      "reject"         broker 422-style rejection
    """
    equity: float = 50_000.0
    settled_cash: float = 50_000.0
    behaviors: dict[str, str] = field(default_factory=dict)
    orders: dict[str, BrokerOrder] = field(default_factory=dict)
    positions: dict[str, BrokerPosition] = field(default_factory=dict)
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    submissions: list[dict] = field(default_factory=list)   # assertion trail
    cancels: list[str] = field(default_factory=list)

    # -- programming interface -------------------------------------------------
    def set_behavior(self, key: str, behavior: str) -> None:
        self.behaviors[key] = behavior

    def fill_order(self, broker_order_id: str, price: float | None = None,
                   qty: int | None = None) -> None:
        """Manually fill a resting order (e.g. a catastrophe stop in a test)."""
        o = self.orders[broker_order_id]
        fill_qty = qty if qty is not None else (o.qty - o.filled_qty)
        px = price if price is not None else (o.limit_price or o.stop_price)
        self._apply_fill(o, fill_qty, px)

    def inject_position(self, ticker: str, qty: int, avg_entry: float) -> None:
        """Reconciliation-drift fixture: a position the DB doesn't know about."""
        self.positions[ticker] = BrokerPosition(ticker, qty, avg_entry)

    def drop_position(self, ticker: str) -> None:
        """Reconciliation-drift fixture: broker lost/closed a position."""
        self.positions.pop(ticker, None)

    # -- internals ---------------------------------------------------------------
    def _behavior_for(self, client_order_id: str, ticker: str) -> str:
        return self.behaviors.get(client_order_id,
                                  self.behaviors.get(ticker, "fill"))

    def _apply_fill(self, o: BrokerOrder, qty: int, price: float) -> None:
        o.filled_qty += qty
        o.filled_avg_price = price
        o.status = "filled" if o.filled_qty >= o.qty else "partially_filled"
        sign = 1 if o.side == "BUY" else -1
        pos = self.positions.get(o.ticker)
        if pos is None and sign > 0:
            self.positions[o.ticker] = BrokerPosition(o.ticker, qty, price)
        elif pos is not None:
            new_qty = pos.qty + sign * qty
            if new_qty <= 0:
                self.positions.pop(o.ticker, None)
            else:
                if sign > 0:
                    pos.avg_entry = ((pos.avg_entry * pos.qty + price * qty)
                                     / new_qty)
                pos.qty = new_qty
        self.settled_cash -= sign * qty * price

    def _submit(self, ticker, side, qty, order_type, limit_price, stop_price,
                client_order_id) -> BrokerOrder:
        # idempotency at the broker: duplicate client_order_id returns original
        for o in self.orders.values():
            if o.client_order_id == client_order_id:
                return o
        behavior = self._behavior_for(client_order_id, ticker)
        self.submissions.append({"ticker": ticker, "side": side, "qty": qty,
                                 "type": order_type, "limit": limit_price,
                                 "stop": stop_price, "coid": client_order_id})
        if behavior == "reject":
            raise BrokerReject(f"scripted reject for {client_order_id}")
        oid = f"fake-{os.urandom(4).hex()}-{next(self._seq)}"
        o = BrokerOrder(broker_order_id=oid, client_order_id=client_order_id,
                        ticker=ticker, side=side, order_type=order_type,
                        qty=qty, limit_price=limit_price, stop_price=stop_price,
                        status="accepted", submitted_ts=utcnow())
        self.orders[oid] = o
        if behavior == "fill" and order_type == "limit":
            self._apply_fill(o, qty, limit_price)
        elif behavior.startswith("partial:") and order_type == "limit":
            self._apply_fill(o, int(behavior.split(":")[1]), limit_price)
        # stops rest by default regardless of behavior (fill via fill_order)
        return o

    # -- Broker interface ----------------------------------------------------------
    async def get_account(self) -> Account:
        return Account(equity=self.equity, settled_cash=self.settled_cash)

    async def get_positions(self) -> list[BrokerPosition]:
        return list(self.positions.values())

    async def get_open_orders(self) -> list[BrokerOrder]:
        return [o for o in self.orders.values() if not o.terminal]

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        return self.orders[broker_order_id]

    async def submit_limit(self, ticker, side, qty, limit_price,
                           client_order_id, tif="day") -> BrokerOrder:
        return self._submit(ticker, side, qty, "limit", limit_price, None,
                            client_order_id)

    async def submit_stop(self, ticker, side, qty, stop_price,
                          client_order_id, tif="gtc") -> BrokerOrder:
        return self._submit(ticker, side, qty, "stop", None, stop_price,
                            client_order_id)

    async def cancel(self, broker_order_id: str) -> bool:
        o = self.orders.get(broker_order_id)
        self.cancels.append(broker_order_id)
        if o is None or o.terminal:
            return False
        o.status = "canceled"
        return True


def get_broker() -> Broker:
    kind = os.environ.get("BROKER", "alpaca").lower()
    if kind == "alpaca":
        return AlpacaBroker()
    if kind == "fake":
        return FakeBroker()
    raise RuntimeError(f"unknown BROKER={kind!r} (expected 'alpaca' or 'fake')")

```

## `src/common/clock.py`

```python
"""UTC discipline (baseline §11.5). Every timestamp the pipeline creates or
parses goes through this module. ET conversion happens only in market-logic
code, and only via market_hours_now() here — never ad hoc.

Market-hours here is deliberately coarse (gap-threshold selection only):
weekday 9:30–16:00 ET. The exchange-calendar library arrives with C3/C4 where
holiday precision is load-bearing; a gap alert on a holiday is a false positive
we tolerate in Phase 1, not a trading error.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    """The only clock the pipeline reads."""
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None = None) -> str:
    """ISO-8601 UTC with milliseconds — the contract timestamp format (spec §3)."""
    dt = dt or utcnow()
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected: all timestamps must be aware")
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(raw: str | int | float | datetime) -> datetime:
    """Parse a source timestamp into an aware UTC datetime.

    Raises ValueError for anything unparseable or naive — callers route those
    items to quarantine with BAD_TIMESTAMP (v0.4: quarantine, never drop).
    """
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raise ValueError(f"naive datetime: {raw!r}")
        return raw.astimezone(timezone.utc)
    if isinstance(raw, (int, float)):
        # epoch seconds or milliseconds; sanity-bounded to 2000–2100
        val = float(raw)
        if val > 1e12:
            val /= 1000.0
        if not 946_684_800 <= val <= 4_102_444_800:
            raise ValueError(f"epoch out of range: {raw!r}")
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise ValueError("empty timestamp")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as e:
            raise ValueError(f"unparseable timestamp: {raw!r}") from e
        if dt.tzinfo is None:
            raise ValueError(f"naive timestamp: {raw!r}")
        return dt.astimezone(timezone.utc)
    raise ValueError(f"unsupported timestamp type: {type(raw)}")


def is_market_hours(dt: datetime | None = None) -> bool:
    """Coarse RTH check for gap-threshold selection (see module docstring)."""
    et = (dt or utcnow()).astimezone(_ET)
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return (9 * 60 + 30) <= minutes < (16 * 60)

```

## `src/common/config.py`

```python
"""Shared YAML config loading. PyYAML if installed; otherwise a tiny built-in
parser for the strict subset our config files use (nested maps by 2-space
indent, lists of scalars or single-level maps, scalar types, # comments).
Moved here from c1_ingestion.service in Phase 2 so all services share it.
"""
from __future__ import annotations

import os


def load_yaml(path: str) -> dict:
    try:
        import yaml  # type: ignore
        with open(path) as f:
            return yaml.safe_load(f)
    except ImportError:
        return _tiny_yaml(path)


def config_path(filename: str) -> str:
    """Resolve a config file relative to the repo's config/ dir, overridable
    per-file via env: sources.yaml -> SOURCES_CONFIG, a1.yaml -> A1_CONFIG."""
    env_key = filename.split(".")[0].upper() + "_CONFIG"
    if os.environ.get(env_key):
        return os.environ[env_key]
    return os.path.join(os.path.dirname(__file__), "..", "..", "config", filename)


class _LazyNode(dict):
    """Starts as dict; converts semantics to list if children are list items."""
    def __init__(self):
        super().__init__()
        self._list: list | None = None

    def append(self, x):
        if self._list is None:
            self._list = []
        self._list.append(x)

    def resolved(self):
        return self._list if self._list is not None else dict(self)


def _tiny_yaml(path: str) -> dict:
    root: dict = {}
    stack: list[tuple[int, dict | list]] = [(-1, root)]
    with open(path) as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            stripped = line.split("#", 1)[0].rstrip() if not line.lstrip().startswith("#") else ""
            if not stripped.strip():
                continue
            indent = len(stripped) - len(stripped.lstrip())
            content = stripped.strip()
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1]
            if content.startswith("- "):
                item_src = content[2:].strip()
                if not hasattr(parent, "append"):
                    raise ValueError(f"list item outside list: {raw_line!r}")
                if ":" in item_src:
                    k, v = item_src.split(":", 1)
                    obj = {k.strip(): _scalar(v.strip())}
                    parent.append(obj)
                    stack.append((indent, obj))
                else:
                    parent.append(_scalar(item_src))
            elif content.endswith(":"):
                key = _scalar(content[:-1].strip())
                node = _LazyNode()
                parent[key] = node
                stack.append((indent, node))
            else:
                k, v = content.split(":", 1)
                parent[_scalar(k.strip())] = _inline_or_scalar(v.strip())
    return _resolve(root)


def _inline_or_scalar(v: str):
    """PyYAML-consistent handling of inline flow maps: 'low: {2: 1, 3: 1}'
    yields {2: 1, 3: 1} with typed (int) keys. One level deep — nested flow
    collections belong in real YAML, install PyYAML for those."""
    if v.startswith("{") and v.endswith("}"):
        inner = v[1:-1].strip()
        if not inner:
            return {}
        out = {}
        for pair in inner.split(","):
            pk, pv = pair.split(":", 1)
            out[_scalar(pk.strip())] = _scalar(pv.strip())
        return out
    return _scalar(v)


def _resolve(node):
    if isinstance(node, _LazyNode):
        node = node.resolved()
    if isinstance(node, dict):
        return {k: _resolve(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v) for v in node]
    return node


def _scalar(s: str):
    s = s.strip().strip('"').strip("'")
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s

```

## `src/common/contracts.py`

```python
"""Pipeline contracts as Pydantic models, mirroring queue-contracts-spec.md v1.0.

These are the *code-side* JSON Schema validation the spec requires (§13):
grammar-constrained decoding enforces contracts on the model side (Phase 2+);
this module enforces them on every code hop. A NewsItem that fails validation
never reaches news.news_items — it goes to quarantine with a reason code.
"""
from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .clock import iso_utc

CONTRACT_NEWS_ITEM = "news_item/1"
CONTRACT_DEDUPED = "signal.dedup/1"
CONTRACT_TRIAGE = "signal.triage/1"


# ---------------------------------------------------------------------------
# §4 NewsItem — mirrors news.news_items one-to-one
# ---------------------------------------------------------------------------

class NewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)          # source-scoped: "alpaca:40892639"
    revision: int = Field(default=1, ge=1)
    is_correction: bool = False
    supersedes: Optional[int] = None

    source: str = Field(min_length=1)           # "alpaca_benzinga"|"edgar"|"rss:<feed>"
    source_tier: Literal[1, 2, 3]
    source_url: Optional[str] = None
    author: Optional[str] = None

    headline: str = Field(min_length=1)
    summary: Optional[str] = None
    content_hash: str = Field(min_length=1)
    raw: Optional[dict] = None

    symbols: list[str] = Field(default_factory=list)   # MAY BE EMPTY (v0.2)
    channels: list[str] = Field(default_factory=list)
    lang: str = "en"

    published_ts: datetime                       # the source's claim
    received_ts: datetime                        # our wall clock

    @field_validator("published_ts", "received_ts")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("naive timestamp")
        return v

    @field_validator("symbols")
    @classmethod
    def _upper_symbols(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if s and s.strip()]

    def payload(self) -> dict:
        """JSON-safe dict for queue payloads (ISO-8601 UTC ms timestamps, spec §3)."""
        d = self.model_dump(exclude={"raw"})
        d["published_ts"] = iso_utc(self.published_ts)
        d["received_ts"] = iso_utc(self.received_ts)
        return d


def content_hash(headline: str, summary: str | None = None, body: str | None = None) -> str:
    """sha256 of normalized headline+summary+body (spec §4).

    Normalization: NFKC, casefold, whitespace collapsed — so trivial
    reformatting by a feed doesn't masquerade as a revision.
    """
    parts = []
    for part in (headline, summary, body):
        if part:
            norm = unicodedata.normalize("NFKC", part).casefold()
            parts.append(" ".join(norm.split()))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# §5 DedupedSignal (C2 -> A1)
# ---------------------------------------------------------------------------

class ClusterInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster_id: int
    is_new_story: bool
    independent_outlets: int = Field(ge=1)
    total_items: int = Field(ge=1)
    similarity_to_canonical: float = Field(ge=0.0, le=1.0)


class DedupedSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: dict                    # NewsItem.payload(), latest revision
    cluster: ClusterInfo


# ---------------------------------------------------------------------------
# §3 Common envelope
# ---------------------------------------------------------------------------

def envelope(msg_schema: str, producer: str, signal_id: str, item_id: str,
             revision: int, body: dict) -> dict:
    return {
        "envelope": {
            "msg_schema": msg_schema,
            "produced_ts": iso_utc(),
            "producer": producer,
            "trace": {"signal_id": signal_id, "item_id": item_id, "revision": revision},
        },
        "body": body,
    }

```

## `src/common/db.py`

```python
"""psycopg3 async pool + small helpers. One pool per process."""
from __future__ import annotations

import json
import os

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


def dsn() -> str:
    v = os.environ.get("PIPELINE_DSN")
    if not v:
        raise RuntimeError("PIPELINE_DSN is not set (see .env.example)")
    return v


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        # queue.claim_next/ack/fail are plpgsql with unqualified table names —
        # they resolve via the session search_path (the validated lifecycle
        # test sets it the same way). journal/news/queue all on the path.
        _pool = AsyncConnectionPool(
            dsn(), min_size=1, max_size=8, open=False,
            kwargs={"options": "-c search_path=public,journal,news,queue"},
        )
        await _pool.open()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def jb(obj) -> Jsonb:
    """JSONB adapter shorthand."""
    return Jsonb(obj)


def as_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

```

## `src/common/invalidation_dsl.py`

```python
"""MIP v1 — Machine-Invalidation Predicate DSL reference implementation.

Three functions matter:
    validate(spec)                      -> raises MIPError if not schema-legal
    compile_predicate(spec, ctx)       -> CompiledPredicate (refs resolved to literals)
    CompiledPredicate.on_bar(bar)      -> None | Fire(action, detail)

Design per invalidation-dsl-spec.md: closed vocabulary, compile-to-numbers at ARM,
risk-reducing actions only, deterministic evaluation (same bars => same fires).
This module is dependency-free by intent; C4 embeds it directly.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional

METRICS = {"close", "low", "high", "last", "vwap", "volume_ratio", "drawdown_r"}
TFS = {"1m", "5m", "15m", "session"}
OPS = {"<", "<=", ">", ">=", "cross_below", "cross_above"}
REFS = {"prenews_price", "entry_price", "initial_stop",
        "prior_day_low", "prior_day_high", "day_open"}

STDLIB: dict[str, dict[str, Any]] = {
    "close_below_prenews": {
        "id": "close_below_prenews",
        "when": {"metric": "close", "tf": "session", "op": "<",
                 "value": {"ref": "prenews_price"}},
        "persist": {"bars": 1},
        "action": {"type": "exit"},
    },
    "session_close_below_prior_low": {
        "id": "session_close_below_prior_low",
        "when": {"metric": "close", "tf": "session", "op": "<",
                 "value": {"ref": "prior_day_low"}},
        "persist": {"bars": 1},
        "action": {"type": "exit"},
    },
    "vwap_loss_on_volume": {
        "id": "vwap_loss_on_volume",
        "when": {"all": [
            {"metric": "close", "tf": "15m", "op": "<", "value": {"ref": "__vwap__"}},
            {"metric": "volume_ratio", "tf": "15m", "op": ">", "value": 1.5},
        ]},
        "persist": {"bars": 2},
        "action": {"type": "tighten_stop", "to": {"ref": "breakeven"}},
        "_params": {"vol_mult": ("when.all.1.value", 1.5)},
    },
    "give_back_from_entry": {
        "id": "give_back_from_entry",
        "when": {"metric": "drawdown_r", "tf": "5m", "op": ">=", "value": 0.75},
        "persist": {"bars": 1},
        "action": {"type": "alert_guard"},
        "_params": {"r": ("when.value", 0.75)},
    },
    "break_of_day_open": {
        "id": "break_of_day_open",
        "when": {"metric": "close", "tf": "15m", "op": "cross_below",
                 "value": {"ref": "day_open"}},
        "persist": {"bars": 1},
        "action": {"type": "alert_guard"},
    },
}


class MIPError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}")


# ---------------------------------------------------------------------------
# Validation (structural — what the JSON Schema enforces on the model side)
# ---------------------------------------------------------------------------

def _validate_cond(c: dict) -> None:
    if set(c) != {"metric", "tf", "op", "value"}:
        raise MIPError("SCHEMA", f"condition keys {sorted(c)}")
    if c["metric"] not in METRICS:
        raise MIPError("SCHEMA", f"metric {c['metric']}")
    if c["tf"] not in TFS:
        raise MIPError("SCHEMA", f"tf {c['tf']}")
    if c["op"] not in OPS:
        raise MIPError("SCHEMA", f"op {c['op']}")
    v = c["value"]
    if isinstance(v, dict):
        if v.get("ref") not in REFS and v.get("ref") != "__vwap__":
            raise MIPError("SCHEMA", f"ref {v.get('ref')}")
        off = v.get("offset_pct", 0)
        if not isinstance(off, (int, float)) or not -0.2 <= off <= 0.2:
            raise MIPError("SCHEMA", f"offset_pct {off}")
    elif not isinstance(v, (int, float)):
        raise MIPError("SCHEMA", f"value {v!r}")


def validate(spec: dict) -> dict:
    """Validate a spec; expand stdlib calls. Returns the expanded custom form."""
    if "std" in spec:
        if spec["std"] not in STDLIB:
            raise MIPError("SCHEMA", f"unknown std {spec['std']}")
        base = _deepcopy(STDLIB[spec["std"]])
        params = spec.get("params", {})
        mapping = base.pop("_params", {})
        for name, val in params.items():
            if name not in mapping:
                raise MIPError("SCHEMA", f"unknown param {name} for {spec['std']}")
            path, _default = mapping[name]
            _set_path(base, path, val)
        base.pop("_params", None)
        return validate(base)

    required = {"id", "when", "action"}
    if not required <= set(spec):
        raise MIPError("SCHEMA", f"missing {required - set(spec)}")
    when = spec["when"]
    conds = when["all"] if "all" in when else [when]
    if not 1 <= len(conds) <= 3:
        raise MIPError("SCHEMA", "1..3 conditions")
    for c in conds:
        _validate_cond(c)
    bars = spec.get("persist", {}).get("bars", 1)
    if not 1 <= bars <= 10:
        raise MIPError("SCHEMA", f"persist.bars {bars}")
    act = spec["action"]
    if act.get("type") not in {"exit", "tighten_stop", "alert_guard"}:
        raise MIPError("SCHEMA", f"action {act}")
    if act.get("type") == "tighten_stop":
        to = act.get("to", {})
        ok = to == {"ref": "breakeven"} or (
            isinstance(to.get("atr_k"), (int, float)) and 0 < to["atr_k"] <= 3)
        if not ok:
            raise MIPError("SCHEMA", f"tighten_stop.to {to}")
    return spec


# ---------------------------------------------------------------------------
# Compile / ARM — resolve refs to literals, run sanity checks
# ---------------------------------------------------------------------------

@dataclass
class ArmContext:
    entry_price: float
    initial_stop: float
    r_unit: float
    prenews_price: Optional[float] = None
    prior_day_low: Optional[float] = None
    prior_day_high: Optional[float] = None
    day_open: Optional[float] = None          # None pre-open; lazy refs allowed
    atr_14: Optional[float] = None
    mark: Optional[float] = None              # current price at ARM


@dataclass
class Bar:
    ts: int
    tf: str
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume_ratio: float


@dataclass
class Fire:
    predicate_id: str
    action: dict
    bar_ts: int
    detail: str


@dataclass
class CompiledCond:
    metric: str
    tf: str
    op: str
    value: Optional[float]          # None => lazy (__vwap__ / day_open pre-open)
    lazy_ref: Optional[str] = None
    entry_price: float = 0.0
    r_unit: float = 1.0
    _prev: Optional[float] = field(default=None, repr=False)

    def eval(self, bar: Bar, day_open: Optional[float]) -> bool:
        target = self.value
        if self.lazy_ref == "__vwap__":
            target = bar.vwap
        elif self.lazy_ref == "day_open":
            if day_open is None:
                return False
            target = day_open
        if self.metric == "drawdown_r":
            m = (self.entry_price - bar.low) / self.r_unit
        elif self.metric == "volume_ratio":
            m = bar.volume_ratio
        elif self.metric in ("close", "last"):
            m = bar.close
        else:
            m = getattr(bar, self.metric, None)
        if m is None or target is None:
            return False
        prev, self._prev = self._prev, m
        if self.op == "<":
            return m < target
        if self.op == "<=":
            return m <= target
        if self.op == ">":
            return m > target
        if self.op == ">=":
            return m >= target
        if self.op == "cross_below":
            return prev is not None and prev >= target and m < target
        if self.op == "cross_above":
            return prev is not None and prev <= target and m > target
        return False


@dataclass
class CompiledPredicate:
    predicate_id: str
    conds: list[CompiledCond]
    persist_bars: int
    action: dict
    compiled_form: dict             # journal this: INVALIDATION_ARMED
    _streak: int = 0
    fired: bool = False

    def on_bar(self, bar: Bar, day_open: Optional[float] = None) -> Optional[Fire]:
        if self.fired:
            return None
        relevant = [c for c in self.conds if c.tf == bar.tf]
        if not relevant:
            return None
        # a bar only advances the streak if EVERY condition at this tf holds and
        # all other-tf conditions held on their most recent bar (single-tf in v1
        # stdlib; mixed-tf ANDs evaluate on the slower tf's cadence)
        ok = all(c.eval(bar, day_open) for c in relevant)
        others = [c for c in self.conds if c.tf != bar.tf]
        if others:      # mixed-tf: require their last evaluation to have been true
            ok = ok and all(c._prev is not None for c in others)
        self._streak = self._streak + 1 if ok else 0
        if self._streak >= self.persist_bars:
            self.fired = True
            return Fire(self.predicate_id, self.action, bar.ts,
                        f"{self.predicate_id} persisted {self._streak} bar(s)")
        return None


def _resolve(value: Any, ctx: ArmContext) -> tuple[Optional[float], Optional[str]]:
    if isinstance(value, (int, float)):
        return float(value), None
    ref = value["ref"]
    if ref == "__vwap__":
        return None, "__vwap__"
    if ref == "day_open" and ctx.day_open is None:
        return None, "day_open"                      # lazy, compiles at 9:30
    raw = getattr(ctx, ref, None)
    if raw is None:
        raise MIPError("UNRESOLVABLE_REF", ref)
    return raw * (1 + value.get("offset_pct", 0)), None


def compile_predicate(spec: dict, ctx: ArmContext) -> CompiledPredicate:
    spec = validate(spec)
    when = spec["when"]
    conds_spec = when["all"] if "all" in when else [when]
    conds, literal_forms = [], []
    for c in conds_spec:
        val, lazy = _resolve(c["value"], ctx)
        conds.append(CompiledCond(c["metric"], c["tf"], c["op"], val, lazy,
                                  entry_price=ctx.entry_price, r_unit=ctx.r_unit))
        literal_forms.append({**c, "value": val if val is not None else f"lazy:{lazy}"})
        # sanity: price-level triggers must be plausible
        if val is not None and c["metric"] in ("close", "low", "high", "last"):
            if val <= 0 or abs(val - ctx.entry_price) > 0.5 * ctx.entry_price:
                raise MIPError("ABSURD_LEVEL", f"{val} vs entry {ctx.entry_price}")
    if spec["action"]["type"] == "exit" and ctx.mark is not None:
        for cc in conds:
            if cc.value is not None and cc.metric in ("close", "last") \
               and cc.op in ("<", "<=") and ctx.mark <= cc.value:
                raise MIPError("IMMEDIATE_FIRE",
                               f"mark {ctx.mark} already beyond {cc.value}")
    persist = spec.get("persist", {}).get("bars", 1)
    if all(c.tf == "session" for c in conds):
        persist = 1                                   # spec §3: session ignores persist
    compiled_form = {"id": spec["id"], "conds": literal_forms,
                     "persist": persist, "action": spec["action"]}
    return CompiledPredicate(spec["id"], conds, persist, spec["action"], compiled_form)


# ---------------------------------------------------------------------------

def _deepcopy(x):
    import copy
    return copy.deepcopy(x)


def _set_path(obj, path: str, val):
    parts = path.split(".")
    for p in parts[:-1]:
        obj = obj[int(p)] if p.isdigit() else obj[p]
    last = parts[-1]
    if last.isdigit():
        obj[int(last)] = val
    else:
        obj[last] = val

```

## `src/common/journal.py`

```python
"""journal.decisions writes + config_version registration.

Conventions enforced here (journal-schema-spec §2):
  * config_version = git SHA of the repo at service start, registered in
    journal.config_versions before the first decision. Outside a git checkout
    (dev/test), a deterministic content hash of config/ is used, prefixed
    "dev-" so real SHAs and dev hashes can't be confused.
  * decisions.payload carries the FULL structured output; promoted columns
    only where filtered/joined on.
  * Writers pass an open connection when the decision must commit atomically
    with something else (A1: decision row + routing fan-out in one tx).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from typing import Optional

import psycopg

from .db import get_pool, jb
from .log import get_logger, kv

log = get_logger("journal")

_config_version: str | None = None


def compute_config_version() -> str:
    """git SHA of HEAD if we're in a checkout; else content hash of config/."""
    repo_root = os.path.join(os.path.dirname(__file__), "..", "..")
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root,
                             capture_output=True, text=True, timeout=5)
        if sha.returncode == 0:
            return sha.stdout.strip()[:12]
    except (OSError, subprocess.TimeoutExpired):
        pass
    h = hashlib.sha256()
    cfg_dir = os.path.join(repo_root, "config")
    for name in sorted(os.listdir(cfg_dir)):
        p = os.path.join(cfg_dir, name)
        if os.path.isfile(p):
            h.update(name.encode())
            h.update(open(p, "rb").read())
    return "dev-" + h.hexdigest()[:10]


async def register_config_version(summary: str = "") -> str:
    """Idempotent — safe to call at every service startup. The version string
    is computed once per process, but the INSERT always runs (ON CONFLICT
    no-op), so the row exists even if the table was reset since the last call."""
    global _config_version
    if _config_version is None:
        _config_version = compute_config_version()
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO journal.config_versions (config_version, summary)
               VALUES (%s, %s) ON CONFLICT (config_version) DO NOTHING""",
            (_config_version, summary[:200]))
    log.info("config version active", extra=kv(config_version=_config_version))
    return _config_version


def active_config_version() -> str:
    if _config_version is None:
        raise RuntimeError("register_config_version() not called at startup")
    return _config_version


async def write_decision(*, signal_id: str, stage: str, agent: str, action: str,
                         item_id: Optional[str] = None,
                         item_revision: Optional[int] = None,
                         ticker: Optional[str] = None,
                         veto_reason: Optional[str] = None,
                         payload: Optional[dict] = None,
                         reason: Optional[str] = None,
                         confidence: Optional[float] = None,
                         model_id: Optional[str] = None,
                         latency_ms: Optional[int] = None,
                         regime_id: Optional[int] = None,
                         derived_from: Optional[int] = None,
                         conn: psycopg.AsyncConnection | None = None) -> int:
    """Insert one decision row; returns decision_id. Pass conn to join an
    existing transaction (decision + routing fan-out must commit together)."""
    sql = """INSERT INTO journal.decisions
             (signal_id, item_id, item_revision, derived_from, ticker, stage,
              agent, action, veto_reason, payload, reason, confidence,
              model_id, latency_ms, config_version, regime_id)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
             RETURNING decision_id"""
    params = (signal_id, item_id, item_revision, derived_from, ticker, stage,
              agent, action, veto_reason, jb(payload or {}), reason, confidence,
              model_id, latency_ms, active_config_version(), regime_id)

    if conn is not None:
        cur = await conn.execute(sql, params)
        return (await cur.fetchone())[0]
    pool = await get_pool()
    async with pool.connection() as c:
        cur = await c.execute(sql, params)
        return (await cur.fetchone())[0]

```

## `src/common/log.py`

```python
"""Structured single-line logging: ts=<utc> level=<..> component=<..> msg=... k=v ...

Plain stdlib logging under systemd (journald captures stdout); no external deps.
"""
from __future__ import annotations

import logging
import sys

from .clock import iso_utc


class _KVFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"ts={iso_utc()} level={record.levelname} component={record.name} msg={record.getMessage()!r}"
        extras = getattr(record, "kv", None)
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += " exc=" + repr(self.formatException(record.exc_info))
        return base


def get_logger(component: str) -> logging.Logger:
    logger = logging.getLogger(component)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_KVFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def kv(**kwargs) -> dict:
    """Usage: log.info("stored item", extra=kv(item_id=..., revision=...))"""
    return {"kv": kwargs}

```

## `src/common/marketdata.py`

```python
"""Market data layer (Phase 3 decision: Alpaca Market Data API, free IEX feed,
behind a provider abstraction).

KNOWN CAVEAT (accepted, deferred-list item): the free feed is IEX-only —
roughly 2-3% of consolidated volume. C3's volume-multiple check computes on a
biased-but-consistent sample; directionally meaningful, absolutely wrong.
Must revisit (SIP feed or Polygon) before real capital.

Providers:
  AlpacaData — httpx against https://data.alpaca.markets (feed=iex).
               Code-complete; smoke test on the Spark (host unreachable from
               the build environment).
  FakeData   — deterministic fixture provider for tests/dev. Bars are
               programmable per symbol; unprogrammed symbols get a flat tape.

All timestamps aware UTC in and out. Bars are plain dicts:
  {"ts": datetime, "open": float, "high": float, "low": float,
   "close": float, "volume": int, "vwap": float}
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

import httpx

from .clock import utcnow
from .log import get_logger

log = get_logger("marketdata")


@dataclass
class Quote:
    price: float
    bid: float
    ask: float
    ts: datetime

    @property
    def spread_bps(self) -> float:
        mid = (self.bid + self.ask) / 2 or 1.0
        return round((self.ask - self.bid) / mid * 10_000, 2)


class MarketData(Protocol):
    async def minute_bars(self, symbol: str, start: datetime, end: datetime) -> list[dict]: ...
    async def daily_bars(self, symbol: str, n: int) -> list[dict]: ...
    async def snapshot(self, symbol: str) -> Quote: ...
    async def prev_close(self, symbol: str) -> float: ...


# ---------------------------------------------------------------------------
# Derived indicators — pure functions over bars (provider-independent)
# ---------------------------------------------------------------------------

def atr14(daily: list[dict]) -> Optional[float]:
    """Wilder ATR(14) over daily bars (needs >= 15)."""
    if len(daily) < 15:
        return None
    trs = []
    for prev, cur in zip(daily[-15:-1], daily[-14:]):
        tr = max(cur["high"] - cur["low"],
                 abs(cur["high"] - prev["close"]),
                 abs(cur["low"] - prev["close"]))
        trs.append(tr)
    return round(sum(trs) / len(trs), 4)


def adv20(daily: list[dict]) -> Optional[float]:
    if len(daily) < 20:
        return None
    return sum(b["volume"] for b in daily[-20:]) / 20


def sma(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def realized_vol(daily: list[dict], n: int = 20) -> Optional[float]:
    """Annualized close-to-close realized volatility over the last n sessions."""
    closes = [b["close"] for b in daily]
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return round(math.sqrt(var) * math.sqrt(252), 4)


def avg_minute_volume(minute: list[dict]) -> Optional[float]:
    if not minute:
        return None
    return sum(b["volume"] for b in minute) / len(minute)


# ---------------------------------------------------------------------------
# Alpaca (IEX feed)
# ---------------------------------------------------------------------------

class AlpacaData:
    BASE = "https://data.alpaca.markets"

    def __init__(self):
        key = os.environ.get("ALPACA_KEY_ID")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    @staticmethod
    def _bar(b: dict) -> dict:
        return {"ts": datetime.fromisoformat(b["t"].replace("Z", "+00:00")),
                "open": float(b["o"]), "high": float(b["h"]), "low": float(b["l"]),
                "close": float(b["c"]), "volume": int(b["v"]),
                "vwap": float(b.get("vw") or b["c"])}

    async def _get(self, path: str, params: dict) -> dict:
        params = {**params, "feed": "iex"}
        async with httpx.AsyncClient(timeout=15.0, headers=self._headers) as client:
            resp = await client.get(f"{self.BASE}{path}", params=params)
            resp.raise_for_status()
            return resp.json()

    async def minute_bars(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        data = await self._get(f"/v2/stocks/{symbol}/bars",
                               {"timeframe": "1Min", "start": start.isoformat(),
                                "end": end.isoformat(), "limit": 10_000,
                                "adjustment": "raw"})
        return [self._bar(b) for b in (data.get("bars") or [])]

    async def daily_bars(self, symbol: str, n: int) -> list[dict]:
        start = utcnow() - timedelta(days=int(n * 1.7) + 10)   # calendar padding
        data = await self._get(f"/v2/stocks/{symbol}/bars",
                               {"timeframe": "1Day", "start": start.isoformat(),
                                "limit": n + 30, "adjustment": "split"})
        bars = [self._bar(b) for b in (data.get("bars") or [])]
        return bars[-n:]

    async def snapshot(self, symbol: str) -> Quote:
        data = await self._get(f"/v2/stocks/{symbol}/snapshot", {})
        q = data.get("latestQuote") or {}
        t = data.get("latestTrade") or {}
        price = float(t.get("p") or q.get("ap") or 0.0)
        return Quote(price=price, bid=float(q.get("bp") or price),
                     ask=float(q.get("ap") or price),
                     ts=datetime.fromisoformat(
                         (t.get("t") or q.get("t")).replace("Z", "+00:00")))

    async def prev_close(self, symbol: str) -> float:
        bars = await self.daily_bars(symbol, 2)
        if not bars:
            raise RuntimeError(f"no daily bars for {symbol}")
        # if the last bar is today's (partial), the previous one is prev close
        last = bars[-1]
        if last["ts"].date() == utcnow().date() and len(bars) >= 2:
            return bars[-2]["close"]
        return last["close"]


# ---------------------------------------------------------------------------
# Fake (tests/dev)
# ---------------------------------------------------------------------------

@dataclass
class FakeData:
    """Programmable fixture provider. set_minute()/set_daily()/set_quote() per
    symbol; unprogrammed symbols get a flat $100 tape with 10k-share bars."""
    _minute: dict[str, list[dict]] = field(default_factory=dict)
    _daily: dict[str, list[dict]] = field(default_factory=dict)
    _quotes: dict[str, Quote] = field(default_factory=dict)
    _prev_close: dict[str, float] = field(default_factory=dict)

    # -- programming interface -------------------------------------------------
    def set_minute(self, symbol: str, bars: list[dict]) -> None:
        self._minute[symbol] = bars

    def set_daily(self, symbol: str, bars: list[dict]) -> None:
        self._daily[symbol] = bars

    def set_quote(self, symbol: str, quote: Quote) -> None:
        self._quotes[symbol] = quote

    def set_prev_close(self, symbol: str, price: float) -> None:
        self._prev_close[symbol] = price

    @staticmethod
    def flat_daily(n: int, close: float = 100.0, volume: int = 1_000_000,
                   end: datetime | None = None) -> list[dict]:
        end = end or utcnow()
        return [{"ts": end - timedelta(days=n - i), "open": close, "high": close * 1.005,
                 "low": close * 0.995, "close": close, "volume": volume, "vwap": close}
                for i in range(n)]

    @staticmethod
    def ramp_minute(start: datetime, minutes: int, start_price: float,
                    end_price: float, volume_each: int) -> list[dict]:
        out = []
        for i in range(minutes):
            p0 = start_price + (end_price - start_price) * i / max(minutes - 1, 1)
            p1 = start_price + (end_price - start_price) * (i + 1) / max(minutes, 1)
            out.append({"ts": start + timedelta(minutes=i), "open": p0,
                        "high": max(p0, p1), "low": min(p0, p1), "close": p1,
                        "volume": volume_each, "vwap": (p0 + p1) / 2})
        return out

    # -- MarketData interface ---------------------------------------------------
    async def minute_bars(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        bars = self._minute.get(symbol)
        if bars is None:
            bars = self.ramp_minute(start, max(int((end - start).total_seconds() // 60), 1),
                                    100.0, 100.0, 10_000)
        return [b for b in bars if start <= b["ts"] <= end]

    async def daily_bars(self, symbol: str, n: int) -> list[dict]:
        return (self._daily.get(symbol) or self.flat_daily(n))[-n:]

    async def snapshot(self, symbol: str) -> Quote:
        return self._quotes.get(symbol) or Quote(price=100.0, bid=99.98,
                                                 ask=100.02, ts=utcnow())

    async def prev_close(self, symbol: str) -> float:
        if symbol in self._prev_close:
            return self._prev_close[symbol]
        daily = self._daily.get(symbol)
        return daily[-1]["close"] if daily else 100.0


def get_marketdata() -> MarketData:
    kind = os.environ.get("MARKETDATA", "alpaca").lower()
    if kind == "alpaca":
        return AlpacaData()
    if kind == "fake":
        return FakeData()
    raise RuntimeError(f"unknown MARKETDATA={kind!r} (expected 'alpaca' or 'fake')")

```

## `src/common/queue.py`

```python
"""Thin wrappers over the queue schema (news-store-schema.sql):
queue.claim_next / queue.ack / queue.fail plus enqueue with
ON CONFLICT (queue_name, dedup_key) DO NOTHING — duplicate enqueue is a no-op
(rule 19: at-least-once + consumer dedup).

LISTEN/NOTIFY: enqueue() NOTIFYs channel "q_<queue_name with dots replaced>";
consumers LISTEN and also poll on a timeout (belt and braces — a NOTIFY sent
while the consumer is disconnected is lost by design, the poll catches it).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import psycopg

from .db import get_pool, jb


def _channel(queue_name: str) -> str:
    return "q_" + queue_name.replace(".", "_")


async def enqueue(queue_name: str, dedup_key: str, payload: dict,
                  priority: int = 100, conn: psycopg.AsyncConnection | None = None) -> bool:
    """Insert a message; returns False if the dedup key already existed.

    Pass `conn` to enqueue inside an existing transaction (C1 does this so the
    news_items insert and the enqueue commit atomically).
    """
    sql = """
        INSERT INTO queue.messages (queue_name, dedup_key, priority, payload)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (queue_name, dedup_key) DO NOTHING
        RETURNING msg_id
    """
    async def _run(c: psycopg.AsyncConnection) -> bool:
        cur = await c.execute(sql, (queue_name, dedup_key, priority, jb(payload)))
        row = await cur.fetchone()
        if row is not None:
            await c.execute(f"NOTIFY {_channel(queue_name)}")
            return True
        return False

    if conn is not None:
        return await _run(conn)
    pool = await get_pool()
    async with pool.connection() as c:
        return await _run(c)


@dataclass
class Message:
    msg_id: int
    queue_name: str
    dedup_key: str
    priority: int
    payload: dict
    attempts: int
    enqueued_ts: datetime


async def claim(queue_name: str, consumer: str) -> Optional[Message]:
    """Claim the next ready message via queue.claim_next (SKIP LOCKED)."""
    pool = await get_pool()
    async with pool.connection() as c:
        cur = await c.execute("SELECT * FROM queue.claim_next(%s, %s)", (queue_name, consumer))
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
        rec = dict(zip(cols, row))
        return Message(
            msg_id=rec["msg_id"], queue_name=rec["queue_name"],
            dedup_key=rec["dedup_key"], priority=rec["priority"],
            payload=rec["payload"], attempts=rec["attempts"],
            enqueued_ts=rec["enqueued_ts"],
        )


async def ack(msg_id: int) -> None:
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("SELECT queue.ack(%s)", (msg_id,))


async def fail(msg_id: int, error: str) -> None:
    """Retry with linear backoff; past max_attempts -> DLQ into news.quarantine."""
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("SELECT queue.fail(%s, %s)", (msg_id, error[:500]))


async def wait_for_message(queue_name: str, timeout_secs: float = 5.0) -> None:
    """Block until a NOTIFY on the queue's channel or timeout. Dedicated
    connection per call site (LISTEN state is per-connection)."""
    import asyncio

    from .db import dsn
    async with await psycopg.AsyncConnection.connect(dsn(), autocommit=True) as c:
        await c.execute(f"LISTEN {_channel(queue_name)}")
        gen = c.notifies()
        try:
            await asyncio.wait_for(gen.__anext__(), timeout=timeout_secs)
        except (asyncio.TimeoutError, StopAsyncIteration):
            pass

```

## `src/router/__init__.py`

```python

```

## `src/router/facts.py`

```python
"""Routing facts — all computed by CODE, never by the model (spec §6).

market_open: pandas-market-calendars NYSE schedule (adopted in Phase 2 by
  decision — holiday-correct from day one). Schedules are cached per day.
position_ids: open positions in journal.positions intersecting the tickers.
  Correct code that returns [] until Phase 4 creates positions.
thesis_matches: STUB returning [] until the Phase 8 thesis store exists.
priority_score: deterministic formula; weights from config/a1.yaml are
  PLACEHOLDERS pending the Phase-4-gating config-values design item.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from common.clock import utcnow
from common.db import get_pool
from common.log import get_logger

log = get_logger("router.facts")

_schedule_cache: dict[str, tuple[datetime, datetime] | None] = {}


def market_open_now(now: datetime | None = None) -> bool:
    """NYSE regular session check, holiday-aware."""
    import pandas_market_calendars as mcal
    now = now or utcnow()
    day_key = now.strftime("%Y-%m-%d")
    if day_key not in _schedule_cache:
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=day_key, end_date=day_key)
        if sched.empty:
            _schedule_cache[day_key] = None          # holiday/weekend
        else:
            _schedule_cache[day_key] = (
                sched.iloc[0]["market_open"].to_pydatetime(),
                sched.iloc[0]["market_close"].to_pydatetime(),
            )
    window = _schedule_cache[day_key]
    if window is None:
        return False
    return window[0] <= now < window[1]


async def open_position_ids(tickers: list[str]) -> list[int]:
    """Open positions intersecting the tickers. Empty until Phase 4."""
    if not tickers:
        return []
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT position_id FROM journal.positions
               WHERE ticker = ANY(%s) AND status = 'OPEN'""",
            (tickers,))
        return [r[0] for r in await cur.fetchall()]


async def thesis_matches(tickers: list[str]) -> list[str]:
    """STUB: the thesis store arrives in Phase 8 (A5). Until then no signal
    matches a standing thesis."""
    return []


def priority_score(source_tier: int, urgency: str, novelty: float,
                   independent_outlets: int, cfg: dict) -> int:
    tier_w = cfg["tier_weight"].get(source_tier, 0)
    urg_w = cfg["urgency_weight"].get(urgency, 0)
    nov = round(novelty * 4)
    corro = min((max(independent_outlets, 1) - 1) * cfg["corroboration_bonus_per_outlet"],
                cfg["corroboration_bonus_cap"])
    return int(tier_w + urg_w + nov + corro)


@dataclass
class RoutingFacts:
    market_open: bool
    position_ids: list[int] = field(default_factory=list)
    thesis_matches: list[str] = field(default_factory=list)
    priority_score: int = 0

    def payload(self) -> dict:
        return {"market_open": self.market_open, "position_ids": self.position_ids,
                "thesis_matches": self.thesis_matches,
                "priority_score": self.priority_score}


async def compute_facts(tickers: list[str], source_tier: int, urgency: str,
                        novelty: float, independent_outlets: int,
                        router_cfg: dict, now: datetime | None = None) -> RoutingFacts:
    return RoutingFacts(
        market_open=market_open_now(now),
        position_ids=await open_position_ids(tickers),
        thesis_matches=await thesis_matches(tickers),
        priority_score=priority_score(source_tier, urgency, novelty,
                                      independent_outlets, router_cfg),
    )

```

## `src/router/rules.py`

```python
"""The four routing rules (queue-contracts-spec §6), deterministic, in order,
as a PURE function — no I/O, trivially testable:

  1. position_ids non-empty -> signal.guard (priority 0), IN ADDITION to
     whatever normal routing produces.
  2. material=false -> DISCARD (journal only); stop.
  3. material=true, no ticker mappable -> signal.thesis (A5 lane);
     never intraday.
  4. market_open -> signal.analyst; else signal.overnight ordered by
     priority_score (queue priority ascending: overnight_base - score).
"""
from __future__ import annotations

from dataclasses import dataclass

from a1_triage.schema import TriageOutput
from .facts import RoutingFacts

GUARD_QUEUE = "signal.guard"
THESIS_QUEUE = "signal.thesis"
ANALYST_QUEUE = "signal.analyst"
OVERNIGHT_QUEUE = "signal.overnight"


@dataclass(frozen=True)
class Route:
    queue: str
    priority: int


@dataclass(frozen=True)
class RoutingDecision:
    action: str                 # ESCALATE | DISCARD
    routes: tuple[Route, ...]   # possibly empty (DISCARD)


def route(triage: TriageOutput, facts: RoutingFacts,
          overnight_base: int = 50) -> RoutingDecision:
    routes: list[Route] = []

    # Rule 1 — guard fan-out happens regardless of the outcome below.
    if facts.position_ids:
        routes.append(Route(GUARD_QUEUE, 0))

    # Rule 2 — not material: journal DISCARD, stop. (Guard fan-out above still
    # applies: a correction touching a held name must reach A12 even if A1
    # scores the item itself immaterial.)
    if not triage.material:
        return RoutingDecision("DISCARD", tuple(routes))

    # Rule 3 — material but no ticker: thesis lane, never intraday.
    if not triage.tickers:
        routes.append(Route(THESIS_QUEUE, 100))
        return RoutingDecision("ESCALATE", tuple(routes))

    # Rule 4 — market-open branch.
    if facts.market_open:
        routes.append(Route(ANALYST_QUEUE, 100))
    else:
        routes.append(Route(OVERNIGHT_QUEUE,
                            max(0, overnight_base - facts.priority_score)))
    return RoutingDecision("ESCALATE", tuple(routes))

```

## `tests/__init__.py`

```python

```

## `tests/integration/__init__.py`

```python

```

## `tests/integration/test_analyst_gate_flow.py`

```python
"""Phase 3 integration against real PostgreSQL 16: the A2 -> C3 chain through
the actual services with FakeData markets and scripted stub models.

Covers: regime snapshot write + reference on A2 decisions; THESIS decision +
signal.gate enqueue; DSL-invalid thesis -> retry -> REJECT; gate PASS ->
GatePass on signal.risk with snapshot; gate CREDIBILITY and GATE_NO_CONFIRM
vetoes journaled with numbers and no message; sympathy lane full round trip
(A2 related_opportunities -> signal.synthetic -> A1 synthetic triage with
derived_from lineage -> signal.analyst with ticker override).
"""
import json
import os
from datetime import timedelta

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"

from common.clock import utcnow
from common.db import get_pool
from common.journal import register_config_version
from common.marketdata import FakeData, Quote
from common.queue import ack, claim, enqueue
from a1_triage.backends import StubBackend
from a1_triage.service import A1Service
from a2_analyst.service import A2Service
from c2_dedup.vectorstore import VectorStore
from c3_gate.service import C3Service

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Deterministic clock: Tuesday 2026-07-07 15:00 UTC = 11:00 ET, in-session.
from datetime import datetime, timezone
PIN = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)
NEWS_TS = PIN - timedelta(minutes=10)

A1_CFG = {"model": {"backend": "stub", "retries_on_invalid": 1},
          "router": {"tier_weight": {1: 6, 2: 4, 3: 1},
                     "urgency_weight": {"high": 6, "medium": 3, "low": 0},
                     "corroboration_bonus_per_outlet": 1,
                     "corroboration_bonus_cap": 3, "overnight_base": 50}}
A2_CFG = {"model": {"backend": "stub", "retries_on_invalid": 1}}
GATE_CFG = {"gate": {"intraday_move_pct": 0.015, "intraday_vol_mult": 2.5,
                     "intraday_window_min": 30, "extended_pct": 0.06,
                     "open_blackout_min": 15, "handoff_gap_ratio": 0.5,
                     "impact_medium_min": 0.02, "impact_high_min": 0.05,
                     "required_outlets": {"low": {2: 1, 3: 1},
                                          "medium": {2: 1, 3: 2},
                                          "high": {2: 2, 3: 3}}}}


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    import shutil
    shutil.rmtree("/tmp/qdrant-test-p3", ignore_errors=True)
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.regime_snapshots, news.cluster_members,
                     news.clusters, news.news_items, queue.messages
                     RESTART IDENTITY CASCADE""")
    await register_config_version("phase3 integration test")
    yield {"pool": pool, "store": VectorStore(path="/tmp/qdrant-test-p3")}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args)
        return await cur.fetchall()


async def seed_item(env, item_id: str, headline: str, tier=2, source="alpaca_benzinga",
                    published=None, outlets_sources=None):
    """Insert an item + cluster directly (C1/C2's output state)."""
    published = published or NEWS_TS
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO news.news_items
               (item_id, revision, source, source_tier, headline, content_hash,
                symbols, channels, published_ts, received_ts)
               VALUES (%s,1,%s,%s,%s,%s,'{ACME}','{}',%s,%s)""",
            (item_id, source, tier, headline, f"hash-{item_id}",
             published, published))
        cur = await c.execute(
            "INSERT INTO news.clusters (canonical_item) VALUES (%s) RETURNING cluster_id",
            (item_id,))
        cluster_id = (await cur.fetchone())[0]
        members = [(item_id, source)] + list(outlets_sources or [])
        for i, (mid, msrc) in enumerate(members):
            if i > 0:
                await c.execute(
                    """INSERT INTO news.news_items
                       (item_id, revision, source, source_tier, headline,
                        content_hash, symbols, channels, published_ts, received_ts)
                       VALUES (%s,1,%s,%s,%s,%s,'{ACME}','{}',%s,%s)""",
                    (mid, msrc, 3, headline, f"hash-{mid}", published, published))
            await c.execute(
                """INSERT INTO news.cluster_members
                   (cluster_id, item_id, revision, source, similarity)
                   VALUES (%s,%s,1,%s,0.95)""", (cluster_id, mid, msrc))
    return cluster_id


def hot_market(now=PIN) -> FakeData:
    """ACME: +2% on 3x volume since 10 minutes ago — a clean intraday confirm."""
    md = FakeData()
    news_ts = now - timedelta(minutes=10)
    md.set_daily("ACME", FakeData.flat_daily(30, close=100.0, volume=5_000_000))
    md.set_prev_close("ACME", 100.0)
    baseline = FakeData.ramp_minute(news_ts - timedelta(days=1), 60, 100.0, 100.0, 10_000)
    since = FakeData.ramp_minute(news_ts, 10, 100.0, 102.0, 30_000)
    md.set_minute("ACME", baseline + since)
    md.set_quote("ACME", Quote(price=102.0, bid=101.98, ask=102.02, ts=now))
    return md


def thesis_reply(**over):
    base = {"ticker": "ACME", "direction": "up", "magnitude_est": 0.055,
            "expected_move_window": "2_sessions", "horizon": "SHORT",
            "confidence": 0.72, "priced_in_assessment": "2% of 5.5% captured",
            "source_risk": "low",
            "invalidation": {"machine_checkable": ["close_below_prenews"],
                             "news_checkable": ["denial"]},
            "related_opportunities": [], "reason": "supply repricing"}
    base.update(over)
    return json.dumps(base)


def triaged_msg(item_id: str, signal_id=None):
    return {"envelope": {"msg_schema": "signal.triaged/1", "producer": "A1",
                         "trace": {"signal_id": signal_id or item_id,
                                   "item_id": item_id, "revision": 1}},
            "body": {"item_ref": {"item_id": item_id, "revision": 1, "cluster_id": 1},
                     "triage": {"material": True, "tickers": ["ACME"],
                                "direction_hint": "up", "urgency": "high",
                                "novelty_score": 0.9, "reason": "t"},
                     "routing": {"market_open": True, "position_ids": [],
                                 "thesis_matches": [], "priority_score": 14}}}


async def process(queue_name, key, payload, svc, handler=None):
    await enqueue(queue_name, key, payload)
    msg = await claim(queue_name, "test")
    assert msg is not None and msg.dedup_key == key
    await (handler or svc.handle)(msg)
    await ack(msg.msg_id)


# ---------------------------------------------------------------------------------

async def test_01_regime_snapshot_written(env):
    from c8_regime.service import write_snapshot
    rid = await write_snapshot(FakeData())
    rows = await q(env, "SELECT features->>'index_trend', features->>'source' "
                        "FROM journal.regime_snapshots WHERE regime_id=%s", rid)
    assert rows[0][0] in ("above_50d", "below_50d")
    assert rows[0][1] == "etf_proxies_iex"


async def test_02_thesis_flow_to_gate_queue(env):
    await seed_item(env, "alpaca:7001", "Acme supply agreement expands",
                    outlets_sources=[("rss:wire:7001", "rss:wire")])
    svc = A2Service(A2_CFG, backend=StubBackend([thesis_reply()]),
                    md=hot_market(), store=env["store"])
    await process("signal.analyst", "alpaca:7001:1", triaged_msg("alpaca:7001"), svc)

    rows = await q(env, """SELECT action, ticker, confidence, regime_id,
                                  payload->'thesis'->>'expected_move_window'
                           FROM journal.decisions
                           WHERE item_id='alpaca:7001' AND stage='ANALYST'""")
    action, ticker, conf, regime_id, window = rows[0]
    assert (action, ticker, window) == ("THESIS", "ACME", "2_sessions")
    assert conf == pytest.approx(0.72)
    assert regime_id is not None                     # references test_01's snapshot

    rows = await q(env, """SELECT payload->'body'->'thesis'->>'magnitude_est'
                           FROM queue.messages
                           WHERE queue_name='signal.gate' AND dedup_key='alpaca:7001:1'""")
    assert rows[0][0] == "0.055"


async def test_03_dsl_invalid_thesis_rejected(env):
    await seed_item(env, "alpaca:7002", "Zed corp wins contract")
    bad = thesis_reply(invalidation={"machine_checkable": ["stock_feels_weak"],
                                     "news_checkable": []})
    svc = A2Service(A2_CFG, backend=StubBackend([bad, bad]),
                    md=hot_market(), store=env["store"])
    await process("signal.analyst", "alpaca:7002:1", triaged_msg("alpaca:7002"), svc)

    rows = await q(env, """SELECT action, payload->>'error' FROM journal.decisions
                           WHERE item_id='alpaca:7002' AND stage='ANALYST'""")
    action, err = rows[0]
    assert action == "REJECT" and "unknown stdlib predicate" in err
    rows = await q(env, "SELECT count(*) FROM queue.messages WHERE queue_name='signal.gate' "
                        "AND dedup_key='alpaca:7002:1'")
    assert rows[0][0] == 0


async def test_04_gate_pass_to_risk_queue(env):
    """Consumes the REAL message A2 enqueued in test_02 — true end-to-end."""
    svc = C3Service(GATE_CFG, md=hot_market(), now_fn=lambda: PIN)
    msg = await claim("signal.gate", "test-c3")
    assert msg is not None and msg.dedup_key == "alpaca:7001:1"
    await svc.handle(msg)
    await ack(msg.msg_id)

    rows = await q(env, """SELECT action, payload->>'pct_move', payload->'snapshot'->>'ref_price'
                           FROM journal.decisions
                           WHERE item_id='alpaca:7001' AND stage='GATE'""")
    action, pct, ref = rows[0]
    assert action == "PASS" and float(pct) == pytest.approx(0.02, abs=0.004)
    assert float(ref) == pytest.approx(102.0)

    rows = await q(env, """SELECT payload->'body'->'gate'->>'verdict',
                                  payload->'body'->'gate'->'snapshot'->>'atr_14'
                           FROM queue.messages
                           WHERE queue_name='signal.risk' AND dedup_key='alpaca:7001:1'""")
    assert rows[0][0] == "PASS" and rows[0][1] is not None


async def test_05_gate_credibility_veto_no_message(env):
    """Tier-3 single-source high-impact claim never passes alone (v0.2)."""
    await seed_item(env, "rss:blog:x1", "MicroCap to be acquired, blog says",
                    tier=3, source="rss:blog")
    svc = C3Service(GATE_CFG, md=hot_market(), now_fn=lambda: PIN)
    gate_msg = {"envelope": {"msg_schema": "signal.gate/1", "producer": "A2",
                             "trace": {"signal_id": "rss:blog:x1",
                                       "item_id": "rss:blog:x1", "revision": 1}},
                "body": {"item_ref": {"item_id": "rss:blog:x1", "revision": 1},
                         "thesis": json.loads(thesis_reply(source_risk="high")),
                         "regime_id": 1}}
    await process("signal.gate", "gate:x1:1", gate_msg, svc)

    rows = await q(env, """SELECT action, veto_reason,
                                  payload->'credibility'->>'required_outlets'
                           FROM journal.decisions
                           WHERE item_id='rss:blog:x1' AND stage='GATE'""")
    assert tuple(rows[0]) == ("VETO", "CREDIBILITY", "3")
    rows = await q(env, "SELECT count(*) FROM queue.messages WHERE queue_name='signal.risk' "
                        "AND dedup_key='gate:x1:1'")
    assert rows[0][0] == 0                       # vetoes produce no message (§8)


async def test_06_gate_no_confirm_veto(env):
    """Corroborated story but flat tape -> GATE_NO_CONFIRM."""
    await seed_item(env, "alpaca:7003", "Acme mid-cycle update", tier=2,
                    outlets_sources=[("rss:wire:7003", "rss:wire")])
    md = FakeData()   # flat default tape: no move, no volume spike
    md.set_daily("ACME", FakeData.flat_daily(30, close=100.0, volume=5_000_000))
    svc = C3Service(GATE_CFG, md=md, now_fn=lambda: PIN)
    gate_msg = {"envelope": {"msg_schema": "signal.gate/1", "producer": "A2",
                             "trace": {"signal_id": "alpaca:7003",
                                       "item_id": "alpaca:7003", "revision": 1}},
                "body": {"item_ref": {"item_id": "alpaca:7003", "revision": 1},
                         "thesis": json.loads(thesis_reply(magnitude_est=0.03)),
                         "regime_id": 1}}
    await process("signal.gate", "gate:7003:1", gate_msg, svc)
    rows = await q(env, """SELECT veto_reason FROM journal.decisions
                           WHERE item_id='alpaca:7003' AND stage='GATE'""")
    assert rows[0][0] == "GATE_NO_CONFIRM"


async def test_07_sympathy_lane_round_trip(env):
    """A2 related_opportunities -> signal.synthetic -> A1 synthetic triage
    (derived_from lineage) -> signal.analyst with ticker override."""
    await seed_item(env, "alpaca:7004", "Acme acquisition confirmed at $45")
    reply = thesis_reply(related_opportunities=[
        {"ticker": "SUPL", "relation": "supplier",
         "rationale": "sole supplier of Acme's key component"}])
    a2 = A2Service(A2_CFG, backend=StubBackend([reply]),
                   md=hot_market(), store=env["store"])
    await process("signal.analyst", "alpaca:7004:1", triaged_msg("alpaca:7004"), a2)

    # synthetic enqueued with lineage
    rows = await q(env, """SELECT payload->'body'->>'synthetic_id',
                                  payload->'body'->>'derived_from_decision'
                           FROM queue.messages WHERE queue_name='signal.synthetic'""")
    syn_id, parent_dec = rows[0]
    assert syn_id.startswith("syn-") and syn_id.endswith("-SUPL")

    # A1 consumes it via handle_synthetic
    a1 = A1Service(A1_CFG, backend=StubBackend(
        [json.dumps({"material": True, "tickers": ["SUPL"], "direction_hint": "up",
                     "urgency": "medium", "novelty_score": 0.6,
                     "reason": "supplier exposure to confirmed deal"})]),
        store=env["store"])
    msg = await claim("signal.synthetic", "test-a1")
    assert msg is not None
    await a1.handle_synthetic(msg)
    await ack(msg.msg_id)

    rows = await q(env, """SELECT signal_id, ticker, derived_from,
                                  payload->>'synthetic'
                           FROM journal.decisions
                           WHERE signal_id = %s AND stage='TRIAGE'""", syn_id)
    sid, ticker, derived_from, synthetic = rows[0]
    assert ticker == "SUPL" and synthetic == "true"
    assert derived_from == int(parent_dec)       # lineage to the A2 decision

    rows = await q(env, """SELECT payload->'envelope'->'trace'->>'ticker'
                           FROM queue.messages
                           WHERE queue_name IN ('signal.analyst','signal.overnight')
                             AND dedup_key = %s""", syn_id)
    assert rows[0][0] == "SUPL"                  # A2 will analyze the sympathy name


async def test_08_gate_and_thesis_share_config_version(env):
    rows = await q(env, """SELECT count(DISTINCT config_version) FROM journal.decisions""")
    assert rows[0][0] == 1

```

## `tests/integration/test_exit_engine_flow.py`

```python
"""Phase 4 chunk-2 integration on live PG16: the exit engine through real
persistence with FakeBroker.

Covers: trail ratchet persisted + events; stop exit with cancel-catastrophe
-> fill -> exits attribution -> position CLOSED; reinstatement when the exit
doesn't fill inside the window; catastrophe-already-filled race; scale-out
re-placing the catastrophe for the remainder; MIP invalidation fired
end-to-end (armed forms journaled); D1 overnight EXIT flow + forced-hold
reprice; drawdown breaker trip on marked losses; dead-man ladder block +
recovery + ownership rule.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"
os.environ["BROKER"] = "fake"

from common.broker import FakeBroker
from common.db import get_pool, jb
from common.journal import register_config_version
from c4_exec.breaker import check_breaker
from c4_exec.deadman import check as deadman_check
from c4_exec.engine import PositionEngine
from c4_exec.flags import ensure_defaults, get_flag, set_flag
from c4_exec.state import open_positions

pytestmark = pytest.mark.asyncio(loop_scope="session")

PIN = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)

POLICY = {
    "profile": "short_term_v1",
    "initial_stop": {"method": "atr", "k": 2.0, "price": 96.0},
    "catastrophe_stop_broker": {"k": 3.5, "price": 93.0},
    "breakeven_at_R": 1.0,
    "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
    "time_stop": {"window": "2_sessions", "min_progress_R": 0.5},
    "realization": {"target_fraction": 0.7, "action": "scale_out_50"},
    "machine_invalidations": ["close_below_prenews"],
    "magnitude_est": 0.055,
    "atr_14": 2.0,
}
ON_CFG = {"hold_min_unrealized_R": 0.3, "young_max_age_sessions": 1,
          "young_max_realized_fraction": 0.5, "check_time_et": "15:45"}


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.regime_snapshots, journal.intents, journal.orders,
                     journal.fills, journal.positions, journal.position_events,
                     journal.exits, journal.audit, journal.health,
                     news.news_items, queue.messages RESTART IDENTITY CASCADE""")
        await c.execute("DELETE FROM journal.control")
    await register_config_version("phase4 chunk2 integration")
    await ensure_defaults()
    await set_flag("trading_capital", "50000", "TEST")
    await set_flag("broker_equity", "48000", "TEST")
    await set_flag("settled_cash", "48000", "TEST")
    return {"pool": pool}


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args)
        return await cur.fetchall()


async def seed_position(env, broker, ticker="ACME", qty=60, avg_entry=100.0,
                        policy_over=None, opened_ts=None,
                        prenews: float = 99.0) -> dict:
    """Create an OPEN position + broker state + resting catastrophe, the way
    chunk-1 entry flow leaves the world. Returns the positions row dict."""
    policy = json.loads(json.dumps(POLICY))
    if policy_over:
        policy.update(policy_over)
    # ArmContext for stdlib close_below_prenews needs prenews_price -- keep it
    # in policy for the engine's ArmContext build (engine reads atr_14; the
    # stdlib predicate resolves prenews via ctx.prenews_price)
    policy["prenews_price"] = prenews
    pool = env["pool"]
    async with pool.connection() as c:
        cur = await c.execute(
            """INSERT INTO journal.decisions (signal_id, stage, agent, action,
                 ticker, reason, config_version)
               SELECT %s,'ANALYST','A2','THESIS',%s,'t', config_version
               FROM journal.config_versions LIMIT 1 RETURNING decision_id""",
            (f"sig:{ticker}", ticker))
        thesis = (await cur.fetchone())[0]
        await c.execute(
            """INSERT INTO journal.intents (intent_id, decision_id, ticker,
                 side, qty, limit_price, status, config_version)
               SELECT %s,%s,%s,'BUY',%s,%s,'FILLED', config_version
               FROM journal.config_versions LIMIT 1""",
            (f"int-{ticker}", thesis, ticker, qty, avg_entry))
        cur = await c.execute(
            """INSERT INTO journal.positions (ticker, horizon, profile, status,
                 opened_ts, entry_intent_id, thesis_decision_id, qty_initial,
                 qty_open, avg_entry, initial_stop, r_unit, exit_policy,
                 config_version)
               SELECT %s,'SHORT','short_term_v1','OPEN',%s,%s,%s,%s,%s,%s,%s,
                      %s,%s, config_version
               FROM journal.config_versions LIMIT 1 RETURNING position_id""",
            (ticker, opened_ts or PIN, f"int-{ticker}", thesis, qty, qty,
             avg_entry, policy["initial_stop"]["price"],
             avg_entry - policy["initial_stop"]["price"], jb(policy)))
        pid = (await cur.fetchone())[0]
    broker.inject_position(ticker, qty, avg_entry)
    stop_order = await broker.submit_stop(ticker, "SELL", qty, 93.0,
                                          client_order_id=f"cat-{ticker}")
    async with pool.connection() as c:
        cur = await c.execute(
            """INSERT INTO journal.orders (position_id, broker_order_id,
                 order_role, state, qty, stop_price)
               VALUES (%s,%s,'CATASTROPHE_STOP','ACCEPTED',%s,93.0)
               RETURNING order_id""", (pid, stop_order.broker_order_id, qty))
        cat_row = (await cur.fetchone())[0]
        await c.execute(
            """UPDATE journal.positions SET catastrophe_stop_order_id=%s
               WHERE position_id=%s""", (cat_row, pid))
    rows = await q(env, "SELECT position_id, ticker, horizon, qty_open, "
                        "avg_entry, r_unit, initial_stop, exit_policy, "
                        "opened_ts, last_price FROM journal.positions "
                        "WHERE position_id=%s", pid)
    r = rows[0]
    return {"position_id": r[0], "ticker": r[1], "horizon": r[2],
            "qty_open": r[3], "avg_entry": float(r[4]), "r_unit": float(r[5]),
            "initial_stop": float(r[6]), "exit_policy": r[7],
            "opened_ts": r[8], "last_price": r[9]}


def engine_for(broker):
    return PositionEngine(broker, now_fn=lambda: PIN,
                          unprotected_max_secs=0.03, poll_sleep=0.01,
                          session_age_fn=lambda o, n: 0)


def bar(o=100.0, h=100.5, l=99.5, c=100.0, **kw):
    return {"ts": int(PIN.timestamp()), "open": o, "high": h, "low": l,
            "close": c, **kw}


# ---------------------------------------------------------------------------

async def test_01_trail_ratchet_persists(env):
    broker = FakeBroker()
    pos = await seed_position(env, broker, "ACME",
                              policy_over={"magnitude_est": 0.15})
    eng = engine_for(broker)                     # target 110.5: no scale-out
    applied = await eng.step(pos, bar(h=106.5, l=105.0, c=106.0))
    assert any(a.startswith("SET_STOP:trail:101.5") for a in applied)
    rows = await q(env, """SELECT exit_policy->>'current_stop',
                                  exit_policy->>'stop_basis',
                                  exit_policy->>'hwm', last_price
                           FROM journal.positions WHERE ticker='ACME'""")
    stop, basis, hwm, mark = rows[0]
    assert (float(stop), basis, float(hwm)) == (101.5, "trail", 106.5)
    assert float(mark) == 106.0
    rows = await q(env, """SELECT event_type, r_progress FROM
                           journal.position_events pe
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='ACME' AND event_type='TRAIL_UPDATED'""")
    assert rows and float(rows[0][1]) == pytest.approx(1.5)


async def test_02_stop_exit_full_mechanics(env):
    """Trail stop hit -> cancel catastrophe -> exit fills -> exits row with
    TRAIL attribution -> position CLOSED -> orders trail complete."""
    broker = FakeBroker()
    rows = await q(env, "SELECT position_id FROM journal.positions WHERE ticker='ACME'")
    # reuse the ACME position; refresh broker state (new FakeBroker)
    broker.inject_position("ACME", 60, 100.0)
    stop_order = await broker.submit_stop("ACME", "SELL", 60, 93.0,
                                          client_order_id="cat-ACME-2")
    async with env["pool"].connection() as c:
        cur = await c.execute(
            """INSERT INTO journal.orders (position_id, broker_order_id,
                 order_role, state, qty, stop_price)
               VALUES (%s,%s,'CATASTROPHE_STOP','ACCEPTED',60,93.0)
               RETURNING order_id""", (rows[0][0], stop_order.broker_order_id))
        await c.execute("""UPDATE journal.positions
                           SET catastrophe_stop_order_id=%s
                           WHERE position_id=%s""",
                        ((await cur.fetchone())[0], rows[0][0]))
    prows = await q(env, "SELECT position_id, ticker, horizon, qty_open, "
                         "avg_entry, r_unit, initial_stop, exit_policy, "
                         "opened_ts, last_price FROM journal.positions "
                         "WHERE ticker='ACME'")
    r = prows[0]
    pos = {"position_id": r[0], "ticker": r[1], "horizon": r[2],
           "qty_open": r[3], "avg_entry": float(r[4]), "r_unit": float(r[5]),
           "initial_stop": float(r[6]), "exit_policy": r[7], "opened_ts": r[8],
           "last_price": r[9]}
    eng = engine_for(broker)
    applied = await eng.step(pos, bar(o=102, h=102, l=101.2, c=101.4,
                                      bid=101.35))
    assert "EXIT:TRAIL:FILLED" in applied

    rows = await q(env, """SELECT exit_layer, qty, price, r_multiple, is_partial
                           FROM journal.exits e
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='ACME'""")
    layer, qty, price, r_mult, is_partial = rows[0]
    assert (layer, qty, is_partial) == ("TRAIL", 60, False)
    assert float(price) == 101.35
    assert float(r_mult) == pytest.approx((101.35 - 100.0) / 4.0, abs=0.01)

    rows = await q(env, "SELECT status, qty_open, realized_pnl "
                        "FROM journal.positions WHERE ticker='ACME'")
    status, qty_open, pnl = rows[0]
    assert (status, qty_open) == ("CLOSED", 0)
    assert float(pnl) == pytest.approx(60 * 1.35, abs=0.1)
    # catastrophe was cancelled at the broker
    assert stop_order.broker_order_id in broker.cancels


async def test_03_exit_reinstates_when_unfilled(env):
    broker = FakeBroker()
    pos = await seed_position(env, broker, "BETA")
    broker.set_behavior("BETA", "rest")             # exit limit won't fill
    eng = engine_for(broker)
    applied = await eng.step(pos, bar(l=95.5, c=96.2, bid=96.1))
    assert "EXIT:STOP:REINSTATED" in applied
    rows = await q(env, """SELECT status, qty_open FROM journal.positions
                           WHERE ticker='BETA'""")
    assert tuple(rows[0]) == ("OPEN", 60)           # still open, protected
    # a NEW catastrophe stop rests at the broker
    stops = [o for o in broker.orders.values()
             if o.order_type == "stop" and not o.terminal]
    assert len(stops) == 1 and stops[0].qty == 60
    rows = await q(env, """SELECT detail FROM journal.position_events pe
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='BETA' AND event_type='GUARD_ACTION'""")
    assert any("EXIT_REINSTATED" in (d or "") for (d,) in rows)


async def test_04_catastrophe_filled_race(env):
    """Cancel fails because the broker stop already filled: record the
    CATASTROPHE exit, close, never submit a redundant exit."""
    broker = FakeBroker()
    pos = await seed_position(env, broker, "GAMA")
    cat_id = [o.broker_order_id for o in broker.orders.values()
              if o.order_type == "stop"][0]
    broker.fill_order(cat_id, price=92.8)           # tier-1 fired on its own
    eng = engine_for(broker)
    applied = await eng.step(pos, bar(l=92.5, c=93.0))
    assert "EXIT:STOP:CATASTROPHE_FILLED" in applied
    rows = await q(env, """SELECT exit_layer, price FROM journal.exits e
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='GAMA'""")
    assert rows[0][0] == "CATASTROPHE"
    assert float(rows[0][1]) == pytest.approx(92.8)
    rows = await q(env, "SELECT status FROM journal.positions WHERE ticker='GAMA'")
    assert rows[0][0] == "CLOSED"
    # exactly one SELL reached the broker: the original stop (no double sell)
    sells = [s for s in broker.submissions if s["side"] == "SELL"]
    assert len(sells) == 1


async def test_05_scale_out_resizes_catastrophe(env):
    broker = FakeBroker()
    pos = await seed_position(env, broker, "DLTA")
    eng = engine_for(broker)
    applied = await eng.step(pos, bar(h=104.0, l=103.0, c=103.9, bid=103.85))
    assert "SCALE_OUT:TARGET:FILLED" in applied
    rows = await q(env, """SELECT exit_layer, qty, is_partial FROM journal.exits e
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='DLTA'""")
    assert tuple(rows[0]) == ("TARGET", 30, True)
    rows = await q(env, "SELECT status, qty_open FROM journal.positions "
                        "WHERE ticker='DLTA'")
    assert tuple(rows[0]) == ("OPEN", 30)
    resting = [o for o in broker.orders.values()
               if o.order_type == "stop" and not o.terminal]
    assert len(resting) == 1 and resting[0].qty == 30   # re-sized protection


async def test_06_mip_invalidation_fires_end_to_end(env):
    broker = FakeBroker()
    pos = await seed_position(env, broker, "EPSN", prenews=99.0)
    eng = engine_for(broker)
    # close_below_prenews is a SESSION predicate: intraday closes below
    # prenews must NOT fire it (that's the whole point of session tf)
    a1 = await eng.step(pos, bar(l=98.6, c=98.7, bid=98.65))
    assert not any(x.startswith("EXIT") for x in a1)
    # session close above prenews: still no fire
    pos2 = [p for p in await open_positions() if p["ticker"] == "EPSN"][0]
    a2 = await eng.step(pos2, bar(l=98.9, c=99.4, bid=99.35, tf="session"))
    assert not any(x.startswith("EXIT") for x in a2)
    # session close BELOW prenews: fires, full exit
    pos3 = [p for p in await open_positions() if p["ticker"] == "EPSN"][0]
    a3 = await eng.step(pos3, bar(l=98.4, c=98.5, bid=98.45, tf="session"))
    assert "EXIT:INVALIDATION:FILLED" in a3
    rows = await q(env, """SELECT event_type FROM journal.position_events pe
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='EPSN' ORDER BY event_id""")
    kinds = [r[0] for r in rows]
    assert "INVALIDATION_ARMED" in kinds and "INVALIDATION_FIRED" in kinds
    rows = await q(env, """SELECT exit_layer FROM journal.exits e
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='EPSN'""")
    assert rows[0][0] == "INVALIDATION"


async def test_07_overnight_exit_and_forced_hold(env):
    # scope: close stragglers from earlier tests (their brokers are gone)
    async with env["pool"].connection() as c:
        await c.execute("""UPDATE journal.positions SET status='CLOSED',
                           closed_ts=now(), qty_open=0 WHERE status='OPEN'""")
    broker = FakeBroker()
    # stale flat position: opened 2 sessions ago, no progress -> D1 EXIT
    pos = await seed_position(env, broker, "ZETA",
                              opened_ts=PIN - timedelta(days=3))
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.positions SET last_price=100.2 "
                        "WHERE ticker='ZETA'")
    eng = PositionEngine(broker, now_fn=lambda: PIN,
                         unprotected_max_secs=0.03, poll_sleep=0.01,
                         session_age_fn=lambda o, n: 2)
    results = await eng.overnight_pass(ON_CFG, pass_label="15:45")
    zeta = [r for r in results if r[0] == "ZETA"][0]
    assert (zeta[1], zeta[2]) == ("EXIT", "stale_flat")
    rows = await q(env, """SELECT exit_layer FROM journal.exits e
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='ZETA'""")
    assert rows[0][0] == "OVERNIGHT"

    # winner holds: fresh position marked +0.5R
    pos2 = await seed_position(env, broker, "ETAA")
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.positions SET last_price=102.0 "
                        "WHERE ticker='ETAA'")
    results = await eng.overnight_pass(ON_CFG, pass_label="15:45")
    etaa = [r for r in results if r[0] == "ETAA"][0]
    assert (etaa[1], etaa[2]) == ("HOLD", "unrealized_R_threshold")

    # forced hold on the 15:55 pass when the exit can't fill
    pos3 = await seed_position(env, broker, "THTA",
                               opened_ts=PIN - timedelta(days=3))
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.positions SET last_price=100.1 "
                        "WHERE ticker='THTA'")
    broker.set_behavior("THTA", "rest")
    eng2 = PositionEngine(broker, now_fn=lambda: PIN,
                          unprotected_max_secs=0.03, poll_sleep=0.01,
                          session_age_fn=lambda o, n: 2)
    await eng2.overnight_pass(ON_CFG, pass_label="15:55")
    rows = await q(env, """SELECT new_value->>'decision'
                           FROM journal.position_events pe
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='THTA'
                             AND event_type='OVERNIGHT_HOLD_DECISION'
                           ORDER BY event_id""")
    decisions = [r[0] for r in rows]
    assert decisions == ["EXIT", "FORCED_HOLD"]
    rows = await q(env, "SELECT status FROM journal.positions WHERE ticker='THTA'")
    assert rows[0][0] == "OPEN"                    # protected, held


async def test_08_drawdown_breaker_trips(env):
    """ETAA reversed hard: mark deep red -> day PnL < -2% of effective."""
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.positions SET last_price=76.0 "
                        "WHERE ticker='ETAA'")     # (76-100)*60 = -1440
    tripped = await check_breaker(0.02)            # threshold -960 on 48k
    assert tripped
    assert await get_flag("drawdown_breaker") == "1"
    rows = await q(env, """SELECT detail FROM journal.audit
                           WHERE action='DRAWDOWN_BREAKER_SET'
                           ORDER BY audit_id DESC LIMIT 1""")
    assert "BREAKER_TRIP" in rows[0][0]
    await set_flag("drawdown_breaker", "0", "TEST", "reset")


async def test_09_deadman_ladder_and_ownership(env):
    from c1_ingestion.heartbeat import set_health
    cfg = {"components": {"ingestion": {"alert_min": 3, "block_entries_min": 10},
                          "marketdata": {"alert_min": 2, "block_entries_min": 2,
                                         "exit_engine_suspend_min": 10}}}
    now = PIN
    # fresh heartbeats -> nothing
    await set_health("ingestion", "OK", "hb")
    await set_health("marketdata", "OK", "hb")
    async with env["pool"].connection() as c:      # make both fresh at PIN
        await c.execute("UPDATE journal.health SET updated_ts=%s", (now,))
    actions = await deadman_check(cfg, now, in_session=True)
    assert actions == {"alerts": [], "block": False, "unblock": False,
                       "exit_suspend": False, "exit_resume": False}

    # ingestion stale 12 min -> ALERT + BLOCK_ENTRIES (deadman-owned)
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.health SET updated_ts=%s "
                        "WHERE component='ingestion'",
                        (now - timedelta(minutes=12),))
    actions = await deadman_check(cfg, now, in_session=True)
    assert actions["block"] and ("ingestion", 12.0) in actions["alerts"]
    assert await get_flag("block_entries") == "1"

    # recovery -> deadman clears ITS OWN block
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.health SET updated_ts=%s "
                        "WHERE component='ingestion'", (now,))
    actions = await deadman_check(cfg, now, in_session=True)
    assert actions["unblock"] and await get_flag("block_entries") == "0"

    # operator block is NEVER cleared by the monitor
    await set_flag("block_entries", "1", "OPERATOR", "manual")
    actions = await deadman_check(cfg, now, in_session=True)
    assert not actions["unblock"] and await get_flag("block_entries") == "1"
    await set_flag("block_entries", "0", "TEST", "reset")

    # marketdata stale 12 min -> exit engine suspend; recovery resumes
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.health SET updated_ts=%s "
                        "WHERE component='marketdata'",
                        (now - timedelta(minutes=12),))
    actions = await deadman_check(cfg, now, in_session=True)
    assert actions["exit_suspend"]
    assert await get_flag("exit_engine_suspended") == "1"
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.health SET updated_ts=%s "
                        "WHERE component='marketdata'", (now,))
    actions = await deadman_check(cfg, now, in_session=True)
    assert actions["exit_resume"]
    assert await get_flag("exit_engine_suspended") == "0"

    # off-hours: stale never escalates, only alerts
    async with env["pool"].connection() as c:
        await c.execute("UPDATE journal.health SET updated_ts=%s",
                        (now - timedelta(minutes=30),))
    actions = await deadman_check(cfg, now, in_session=False)
    assert actions["alerts"] and not actions["block"] \
        and not actions["exit_suspend"]


async def test_10_halt_heuristic_freeze_resume(env):
    broker = FakeBroker()
    pos = await seed_position(env, broker, "IOTA")
    clock = {"now": PIN}
    eng = PositionEngine(broker, now_fn=lambda: clock["now"],
                         unprotected_max_secs=0.03, poll_sleep=0.01,
                         halt_stale_min=10.0, session_age_fn=lambda o, n: 0)
    await eng.step(pos, bar())                      # bar seen at PIN
    clock["now"] = PIN + timedelta(minutes=12)      # 12 min of silence
    assert await eng.check_halt(pos) is True
    rows = await q(env, """SELECT event_type FROM journal.position_events pe
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='IOTA'
                             AND event_type IN ('HALT_FROZEN','HALT_RESUMED')
                           ORDER BY event_id""")
    assert [r[0] for r in rows] == ["HALT_FROZEN"]
    # bar returns -> resume
    prows = [p for p in await open_positions() if p["ticker"] == "IOTA"]
    await eng.step(prows[0], bar())
    rows = await q(env, """SELECT event_type FROM journal.position_events pe
                           JOIN journal.positions p USING (position_id)
                           WHERE p.ticker='IOTA'
                             AND event_type IN ('HALT_FROZEN','HALT_RESUMED')
                           ORDER BY event_id""")
    assert [r[0] for r in rows] == ["HALT_FROZEN", "HALT_RESUMED"]

```

## `tests/integration/test_lifecycle.py`

```python
"""Full C1 -> C2 lifecycle against a REAL PostgreSQL 16 instance.

Mirrors the story of news-lifecycle-test.sql, but through the actual service
code: normalize -> store (transactional enqueue) -> C2 consume -> dedup/
cluster/corroborate -> DedupedSignal on signal.triage. Plus: revision flow,
quarantine, duplicate-echo no-op, DLQ after max_attempts, 48h prune.

Requires PIPELINE_DSN pointing at a database with schema/*.sql applied.
Tables are truncated per test session; run against a dev DB only.
"""
import asyncio
import os

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ.setdefault("QDRANT_PATH", "/tmp/qdrant-test")

from common.clock import utcnow
from common.db import get_pool
from common.queue import claim, enqueue, fail as qfail
from c1_ingestion.normalize import NormalizeError, normalize_alpaca, normalize_rss
from c1_ingestion.store import quarantine, store_item
from c2_dedup.cluster import Deduper
from c2_dedup.embedder import get_embedder
from c2_dedup.service import handle_message
from c2_dedup.vectorstore import VectorStore

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    import shutil
    shutil.rmtree("/tmp/qdrant-test", ignore_errors=True)
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE news.cluster_members, news.clusters, news.news_items,
                     news.quarantine, news.ingestion_gaps, queue.messages
            RESTART IDENTITY CASCADE""")
    store = VectorStore(path="/tmp/qdrant-test")
    deduper = Deduper(store, get_embedder())
    yield {"pool": pool, "store": store, "deduper": deduper}


def alpaca_payload(aid: int, headline: str, summary: str = "", symbols=None):
    return {"T": "n", "id": aid, "headline": headline, "summary": summary,
            "created_at": utcnow().isoformat(), "symbols": symbols or [],
            "url": f"https://example.com/{aid}", "source": "benzinga"}


async def drain_and_process(env, n: int):
    """Claim n messages from signal.dedup and run them through C2."""
    results = []
    for _ in range(n):
        msg = await claim("signal.dedup", "test-c2")
        assert msg is not None, "expected a message on signal.dedup"
        await handle_message(msg, env["deduper"])
        from common.queue import ack
        await ack(msg.msg_id)
        results.append(msg)
    return results


# -------------------------------------------------------------------------------
async def test_01_store_and_transactional_enqueue(env):
    item = normalize_alpaca(alpaca_payload(1001, "Acme Corp announces $2B buyback",
                                           "Board approves repurchase program.", ["ACME"]))
    res = await store_item(item)
    assert res.stored and res.revision == 1 and res.enqueued

    async with env["pool"].connection() as c:
        cur = await c.execute("SELECT count(*) FROM news.news_items WHERE item_id='alpaca:1001'")
        assert (await cur.fetchone())[0] == 1
        cur = await c.execute(
            "SELECT count(*) FROM queue.messages WHERE queue_name='signal.dedup' AND dedup_key='alpaca:1001:1'")
        assert (await cur.fetchone())[0] == 1


async def test_02_duplicate_echo_is_noop(env):
    """Feed replay after reconnect: same item, same content -> nothing written."""
    item = normalize_alpaca(alpaca_payload(1001, "Acme Corp announces $2B buyback",
                                           "Board approves repurchase program.", ["ACME"]))
    res = await store_item(item)
    assert not res.stored and not res.enqueued
    async with env["pool"].connection() as c:
        cur = await c.execute("SELECT count(*) FROM news.news_items WHERE item_id='alpaca:1001'")
        assert (await cur.fetchone())[0] == 1


async def test_03_changed_content_becomes_revision(env):
    """v0.4: a correction is a new revision of the same item_id."""
    item = normalize_alpaca(alpaca_payload(1001, "CORRECTED: Acme buyback is $1B, not $2B",
                                           "Board approves repurchase program.", ["ACME"]))
    res = await store_item(item)
    assert res.stored and res.revision == 2 and res.is_correction

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT revision, is_correction, supersedes FROM news.news_items_latest WHERE item_id='alpaca:1001'")
        rev, is_corr, sup = await cur.fetchone()
        assert (rev, is_corr, sup) == (2, True, 1)


async def test_04_quarantine_never_drop(env):
    try:
        normalize_alpaca({"T": "n", "id": 9999, "headline": "x", "created_at": "0000-00-00"})
        assert False
    except NormalizeError as e:
        await quarantine(e, "alpaca_benzinga")
    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT reason_code FROM news.quarantine WHERE source='alpaca_benzinga'")
        assert (await cur.fetchone())[0] == "BAD_TIMESTAMP"


async def test_05_c2_new_story_cluster(env):
    """First unique story -> new cluster, is_new_story=true, forwarded to triage."""
    await drain_and_process(env, 2)   # rev 1 + rev 2 of alpaca:1001

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT payload->'body'->'cluster'->>'is_new_story' FROM queue.messages "
            "WHERE queue_name='signal.triage' AND dedup_key='alpaca:1001:1'")
        assert (await cur.fetchone())[0] == "true"
        # revision 2 joined revision 1's cluster, not a new one
        cur = await c.execute("SELECT count(*) FROM news.clusters")
        assert (await cur.fetchone())[0] == 1
        cur = await c.execute(
            "SELECT payload->'body'->'cluster'->>'is_new_story' FROM queue.messages "
            "WHERE queue_name='signal.triage' AND dedup_key='alpaca:1001:2'")
        assert (await cur.fetchone())[0] == "false"


async def test_06_corroboration_across_independent_outlets(env):
    """Same story from a second outlet -> same cluster, independent_outlets=2.
    This is C3's credibility input (v0.2).

    2026-07-14 change: this outlet's text is IDENTICAL to the first (wire
    copy, sim >= 0.90) so it is a DUPLICATE — corroboration is recorded but
    the item is dropped, not forwarded (baseline §4 C2; the EDGAR-storm fix).
    Distinct-wording corroboration (0.80-0.90 band) still forwards — that is
    test_06b below."""
    rss_entry = {
        "title": "Acme Corp announces $2B buyback",
        "id": "https://wire.example/acme-buyback",
        "link": "https://wire.example/acme-buyback",
        "published": utcnow().isoformat(),
        "summary": "Board approves repurchase program.",
    }
    item = normalize_rss(rss_entry, feed_name="wire")
    await store_item(item)
    await drain_and_process(env, 1)

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT independent_outlets, total_items FROM news.cluster_corroboration WHERE cluster_id=1")
        outlets, total = await cur.fetchone()
        assert outlets == 2, f"expected 2 independent outlets, got {outlets}"
        assert total == 3                       # 2 alpaca revisions + 1 rss

        # duplicate (sim >= 0.90) must NOT reach triage
        cur = await c.execute(
            "SELECT count(*) FROM queue.messages "
            "WHERE queue_name='signal.triage' AND dedup_key LIKE 'rss:wire%'")
        assert (await cur.fetchone())[0] == 0


async def test_06b_distinct_wording_corroboration_still_forwards(env):
    """A second outlet with its own wording (0.80 <= sim < 0.90) is
    corroboration, not a duplicate — it joins the cluster AND forwards."""
    rss_entry = {
        # measured at sim 0.866 to the alpaca:1001 text under the hash
        # embedder — inside the 0.80-0.90 corroboration band
        "title": "Acme Corp announces $2B buyback",
        "id": "https://wire2.example/acme-repurchase",
        "link": "https://wire2.example/acme-repurchase",
        "published": utcnow().isoformat(),
        "summary": "Board approves repurchase program, shares to rise.",
    }
    item = normalize_rss(rss_entry, feed_name="wire2")
    await store_item(item)
    msgs = await drain_and_process(env, 1)

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT payload->'body'->'cluster' FROM queue.messages "
            "WHERE queue_name='signal.triage' AND dedup_key LIKE 'rss:wire2%'")
        row = await cur.fetchone()
        assert row is not None, "corroboration-band item must forward"
        cluster = row[0]
        assert cluster["is_new_story"] is False
        assert cluster["cluster_id"] == 1
        assert cluster["independent_outlets"] == 3


async def test_07_unrelated_story_new_cluster(env):
    item = normalize_alpaca(alpaca_payload(
        2002, "Zenith Pharma wins FDA approval for migraine drug",
        "Phase 3 data cleared.", ["ZNTH"]))
    await store_item(item)
    await drain_and_process(env, 1)
    async with env["pool"].connection() as c:
        cur = await c.execute("SELECT count(*) FROM news.clusters")
        assert (await cur.fetchone())[0] == 2


async def test_08_dlq_to_quarantine_after_max_attempts(env):
    """A poison message retries with backoff then dead-letters into
    news.quarantine with a queue: source prefix (spec §1)."""
    await enqueue("signal.dedup", "poison:1", {"envelope": {}, "body": {"item_id": "poison"}})
    for attempt in range(5):
        async with env["pool"].connection() as c:
            await c.execute(
                "UPDATE queue.messages SET available_ts = now() WHERE dedup_key='poison:1'")
        msg = await claim("signal.dedup", "test-c2")
        assert msg is not None and msg.dedup_key == "poison:1"
        try:
            await handle_message(msg, env["deduper"])
            assert False, "poison message should fail"
        except Exception as e:
            await qfail(msg.msg_id, repr(e))

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT count(*) FROM news.quarantine WHERE source='queue:signal.dedup'")
        assert (await cur.fetchone())[0] == 1
        cur = await c.execute(
            "SELECT done_ts IS NOT NULL FROM queue.messages WHERE dedup_key='poison:1'")
        assert (await cur.fetchone())[0] is True


async def test_09_prune_respects_window(env):
    """Points older than 48h are pruned; fresh ones survive."""
    store = env["store"]
    n_before = store.client.count(store.dedup).count
    assert n_before >= 4                        # everything ingested so far
    # age one point artificially
    pts = store.client.scroll(store.dedup, limit=1)[0]
    store.client.set_payload(store.dedup, payload={"ts": 0.0},
                             points=[pts[0].id])
    removed = store.prune_dedup(48)
    assert removed == 1
    assert store.client.count(store.dedup).count == n_before - 1


async def test_10_gap_monitor_opens_and_closes(env):
    from c1_ingestion.heartbeat import GapMonitor
    from datetime import timedelta
    mon = GapMonitor("testsource", market_threshold_secs=1, offhours_threshold_secs=1)
    mon.last_item_ts = utcnow() - timedelta(seconds=10)
    await mon.check()
    assert mon.open_gap_id is not None
    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT gap_end FROM news.ingestion_gaps WHERE source='testsource'")
        assert (await cur.fetchone())[0] is None          # ongoing
    mon.mark_activity()
    await mon.check()
    assert mon.open_gap_id is None
    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT gap_end IS NOT NULL FROM news.ingestion_gaps WHERE source='testsource'")
        assert (await cur.fetchone())[0] is True          # closed


async def test_11_health_rows_written(env):
    from c1_ingestion.heartbeat import set_health
    await set_health("ingestion", "OK", "test")
    async with env["pool"].connection() as c:
        cur = await c.execute("SELECT status FROM journal.health WHERE component='ingestion'")
        assert (await cur.fetchone())[0] == "OK"

```

## `tests/integration/test_edgar_storm_regression.py`

```python
"""Regression suite for the 2026-07-14 EDGAR revision-storm incident.

Observed on the Spark: EDGAR's current-events index lists one filing once per
associated entity. The SCHEDULE 13G accession 0002141255-26-000001 appeared
as alternating rows —

    "SCHEDULE 13G - Bakhashwain Mohammed (0002141255) (Filed by)"
    "SCHEDULE 13G - Bitzero Holdings Inc. (0002100457) (Subject)"

— and store_item's latest-hash comparison minted a revision on EVERY poll
cycle (rev 58+ observed on one filing; ~80k items/day; A1 permanently
saturated; GPU pinned at 96%). C2 detected the duplicates (sim>=0.90) and
forwarded them anyway.

These tests replay that exact data and assert the three fixes:
  1. one item per accession, immutable — repeat polls are no-ops
  2. C2 drops duplicate verdicts instead of forwarding
  3. non-whitelisted forms (Form 4, 424B2) are archived but never enqueued
"""
import os

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ.setdefault("QDRANT_PATH", "/tmp/qdrant-storm-test")

from common.clock import utcnow
from common.db import get_pool
from common.queue import ack, claim
from c1_ingestion.normalize import edgar_accession, edgar_title_parts, normalize_edgar
from c1_ingestion.sources.edgar import (DEFAULT_TRIAGE_FORMS, EdgarSource,
                                        form_whitelisted)
from c1_ingestion.store import store_item
from c2_dedup.cluster import Deduper
from c2_dedup.embedder import get_embedder
from c2_dedup.service import handle_message
from c2_dedup.vectorstore import VectorStore

pytestmark = pytest.mark.asyncio(loop_scope="session")

ACC = "0002141255-26-000001"


def edgar_entry(title: str, acc: str = ACC) -> dict:
    return {
        "title": title,
        "id": f"urn:tag:sec.gov,2008:accession-number={acc}",
        "link": f"https://www.sec.gov/Archives/edgar/data/2100457/{acc.replace('-','')}-index.htm",
        "updated": utcnow().isoformat(),
        "summary": f"<b>Filed:</b> 2026-07-14 <b>AccNo:</b> {acc} <b>Size:</b> 6 KB",
    }


FILED_BY = edgar_entry("SCHEDULE 13G - Bakhashwain Mohammed (0002141255) (Filed by)")
SUBJECT = edgar_entry("SCHEDULE 13G - Bitzero Holdings Inc. (0002100457) (Subject)")


class _FakeMonitor:
    def mark_activity(self):
        pass


def make_source(triage_forms=None) -> EdgarSource:
    os.environ.setdefault("EDGAR_CONTACT", "test@example.com")
    cfg = {"tier": 1, "poll_interval_secs": 15,
           "feed_url": "https://example.invalid/atom"}
    if triage_forms is not None:
        cfg["triage_forms"] = triage_forms
    return EdgarSource(cfg, _FakeMonitor())


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    import shutil
    shutil.rmtree("/tmp/qdrant-storm-test", ignore_errors=True)
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE news.cluster_members, news.clusters, news.news_items,
                     news.quarantine, news.ingestion_gaps, queue.messages
            RESTART IDENTITY CASCADE""")
    store = VectorStore(path="/tmp/qdrant-storm-test")
    deduper = Deduper(store, get_embedder())
    yield {"pool": pool, "store": store, "deduper": deduper}


# ---------------------------------------------------------------------------
# Unit-level: parsing fixes
# ---------------------------------------------------------------------------

def test_multiword_form_parses():
    """Old regex failed 'SCHEDULE 13G/A' titles entirely (5.5k/day landed in
    the bare {filing} bucket with no form channel)."""
    form, name, cik, role = edgar_title_parts(
        "SCHEDULE 13G/A - Voss Capital, LP (0001730145) (Filed by)")
    assert form == "SCHEDULE 13G/A"
    assert name == "Voss Capital, LP"
    assert cik == "0001730145"
    assert role == "Filed by"


def test_singleword_form_still_parses():
    form, name, cik, role = edgar_title_parts(
        "8-K - CONSTELLATION ENERGY GENERATION LLC (0001168165) (Filer)")
    assert form == "8-K" and cik == "0001168165" and role == "Filer"


def test_accession_extraction():
    assert edgar_accession(FILED_BY) == ACC


def test_form_whitelist_prefix_semantics():
    wl = DEFAULT_TRIAGE_FORMS
    assert form_whitelisted("8-K", wl)
    assert form_whitelisted("8-K/A", wl)           # amendment admitted by prefix
    assert form_whitelisted("SCHEDULE 13G/A", wl)
    assert form_whitelisted("SC 13D/A", wl)
    assert not form_whitelisted("4", wl)           # the 44k/day offender
    assert not form_whitelisted("424B2", wl)       # the 15k/day offender
    assert not form_whitelisted("144", wl)
    assert not form_whitelisted("13F-HR", wl)
    assert not form_whitelisted(None, wl)
    # "4" must not admit "424B2" if someone ever whitelists Form 4
    assert not form_whitelisted("424B2", ["4"])


def test_merge_group_prefers_company_row():
    src = make_source()
    item = src._merge_group([dict(FILED_BY), dict(SUBJECT)])
    # Subject (the company) outranks Filed-by (the person)
    assert item.item_id == f"edgar:{ACC}"
    assert "Bitzero" in item.headline
    ents = item.raw["entities"]
    assert len(ents) == 2
    assert {e["role"] for e in ents} == {"Subject", "Filed by"}
    assert {e["cik"] for e in ents} == {"0002100457", "0002141255"}


# ---------------------------------------------------------------------------
# Integration: the ping-pong replay (fix 1)
# ---------------------------------------------------------------------------

async def test_pingpong_replay_one_item_one_revision(env):
    """The incident, exactly: alternating entity rows across repeated polls.
    Must produce ONE row, revision 1, ONE enqueue — not rev 58."""
    src = make_source()

    # Poll cycle 1: both entity rows arrive together (grouped -> one item)
    item = src._merge_group([dict(FILED_BY), dict(SUBJECT)])
    r1 = await store_item(item, immutable=True, enqueue=True)
    assert r1.stored and r1.revision == 1 and r1.enqueued

    # Poll cycles 2..11: index re-lists the same rows; order flips sometimes
    for i in range(10):
        rows = [dict(SUBJECT), dict(FILED_BY)] if i % 2 else [dict(FILED_BY), dict(SUBJECT)]
        again = src._merge_group(rows)
        r = await store_item(again, immutable=True, enqueue=True)
        assert not r.stored, f"poll {i+2} minted a revision — storm regression"
        assert not r.enqueued

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT count(*), max(revision) FROM news.news_items WHERE item_id=%s",
            (f"edgar:{ACC}",))
        count, max_rev = await cur.fetchone()
        assert count == 1 and max_rev == 1
        cur = await c.execute(
            "SELECT count(*) FROM queue.messages WHERE dedup_key LIKE %s",
            (f"edgar:{ACC}%",))
        assert (await cur.fetchone())[0] == 1


async def test_immutable_beats_even_changed_content(env):
    """Even a genuinely different text under the same accession is a no-op —
    a filing is immutable; changes arrive as new accessions."""
    acc = "0009999999-26-000042"
    e1 = edgar_entry("8-K - ACME CORP (0001234567) (Filer)", acc)
    item1 = normalize_edgar(e1)
    r1 = await store_item(item1, immutable=True)
    assert r1.stored and r1.revision == 1

    e2 = edgar_entry("8-K - ACME CORPORATION LIMITED (0001234567) (Filer)", acc)
    item2 = normalize_edgar(e2)
    assert item2.content_hash != item1.content_hash
    r2 = await store_item(item2, immutable=True)
    assert not r2.stored and r2.revision == 1


async def test_mutable_sources_unaffected(env):
    """Alpaca/RSS revision semantics unchanged: changed hash still revisions."""
    from c1_ingestion.normalize import normalize_alpaca
    pay = {"T": "n", "id": 9901, "headline": "Widget Corp guidance raised",
           "summary": "Q3 outlook up.", "created_at": utcnow().isoformat(),
           "symbols": ["WDGT"], "url": "https://example.com/9901",
           "source": "benzinga"}
    r1 = await store_item(normalize_alpaca(pay))
    assert r1.stored and r1.revision == 1
    pay["headline"] = "Widget Corp guidance raised sharply"
    r2 = await store_item(normalize_alpaca(pay))
    assert r2.stored and r2.revision == 2 and r2.is_correction


# ---------------------------------------------------------------------------
# Integration: form whitelist down-routing (fix 3)
# ---------------------------------------------------------------------------

async def test_form4_archived_but_not_enqueued(env):
    """The 44k/day Form 4 flood: stored as a record, never enters the queue."""
    acc = "0001213900-26-077739"
    entry = edgar_entry("4 - Plum Acquisition Corp, IV (0002030482) (Issuer)", acc)
    src = make_source()
    item = src._merge_group([entry])
    allow = form_whitelisted(item.raw.get("form"), src.triage_forms)
    assert not allow
    r = await store_item(item, immutable=True, enqueue=allow)
    assert r.stored and not r.enqueued

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT count(*) FROM news.news_items WHERE item_id=%s", (f"edgar:{acc}",))
        assert (await cur.fetchone())[0] == 1
        cur = await c.execute(
            "SELECT count(*) FROM queue.messages WHERE dedup_key LIKE %s", (f"edgar:{acc}%",))
        assert (await cur.fetchone())[0] == 0


async def test_8k_whitelisted_and_enqueued(env):
    acc = "0001168165-26-000099"
    entry = edgar_entry("8-K - CONSTELLATION ENERGY GENERATION LLC (0001168165) (Filer)", acc)
    src = make_source()
    item = src._merge_group([entry])
    allow = form_whitelisted(item.raw.get("form"), src.triage_forms)
    assert allow
    r = await store_item(item, immutable=True, enqueue=allow)
    assert r.stored and r.enqueued
    assert "8-K" in item.channels          # router convenience channel intact


# ---------------------------------------------------------------------------
# Integration: C2 drops duplicates (fix 2)
# ---------------------------------------------------------------------------

async def _run_c2_once(env):
    msg = await claim("signal.dedup", "test-c2-storm")
    assert msg is not None
    await handle_message(msg, env["deduper"])
    await ack(msg.msg_id)
    return msg


async def test_c2_drops_duplicate_forwards_original(env):
    """Two distinct items with near-identical text (two 13G rows that slipped
    grouping, or the same story twice): first forwards, duplicate does not."""
    from c1_ingestion.normalize import normalize_rss
    text = "Bitzero Holdings SCHEDULE 13G beneficial ownership disclosure filed today"
    a = normalize_rss({"title": text, "id": "https://w.example/a",
                       "link": "https://w.example/a",
                       "published": utcnow().isoformat(),
                       "summary": "Ownership stake disclosed."}, feed_name="w1")
    b = normalize_rss({"title": text, "id": "https://w.example/b",
                       "link": "https://w.example/b",
                       "published": utcnow().isoformat(),
                       "summary": "Ownership stake disclosed."}, feed_name="w2")
    assert a.item_id != b.item_id           # distinct items, identical text

    # Drain messages left on signal.dedup by earlier tests — claim() is FIFO
    # and would hand us those instead of ours (the known fixture-poisoning
    # pattern from the Phase 4 suite).
    while True:
        stale = await claim("signal.dedup", "test-c2-storm-drain")
        if stale is None:
            break
        await ack(stale.msg_id)

    await store_item(a)
    await store_item(b)
    await _run_c2_once(env)                 # a -> new story, forwards
    await _run_c2_once(env)                 # b -> duplicate, drops

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT count(*) FROM queue.messages WHERE queue_name='signal.triage' "
            "AND dedup_key LIKE 'rss:%'")
        n_triage = (await cur.fetchone())[0]
        assert n_triage == 1, f"duplicate reached triage (got {n_triage})"
        # corroboration still recorded for C3's credibility input
        cur = await c.execute(
            """SELECT cm.cluster_id, count(*) FROM news.cluster_members cm
               JOIN news.news_items ni ON ni.item_id = cm.item_id
               WHERE ni.source LIKE 'rss:%' GROUP BY 1""")
        rows = await cur.fetchall()
        assert rows and rows[0][1] == 2, "duplicate membership not recorded"
```

## `tests/integration/test_risk_exec_flow.py`

```python
"""Phase 4 chunk-1 integration against real PostgreSQL 16: A3 + C4 through
the actual services with FakeBroker and stub discretion.

Covers: RISK SIZE decision + intents row + exec.intent enqueue in one tx;
discretion fallback on invalid model output; RISK vetoes (KILL_SWITCH via
control table, SIZE_CLIPPED via heat); C4 entry fill -> position + two-tier
stops (catastrophe re-materialized off ACTUAL fill); duplicate intent replay
no-op at C4; unfilled entry expiry; broker reject; reconciliation drift
(CLOSED_EXTERNAL + ADOPTED + qty snap) and capital-row refresh; A3 heat
computation reflecting the open position.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ["MARKETDATA"] = "fake"
os.environ["BROKER"] = "fake"

from common.broker import FakeBroker
from common.clock import utcnow
from common.db import get_pool
from common.journal import register_config_version
from common.queue import ack, claim, enqueue
from a1_triage.backends import StubBackend
from a3_risk.service import A3Service
from c4_exec.flags import ensure_defaults, set_flag
from c4_exec.reconcile import reconcile
from c4_exec.service import C4Service

pytestmark = pytest.mark.asyncio(loop_scope="session")

PIN = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)   # Tue, in-session

RISK_CFG = {
    "capital": {"risk_per_trade_pct": 0.005, "max_position_notional_pct": 0.15,
                "max_portfolio_heat_pct": 0.03,
                "heat_split": {"SHORT": 0.02, "LONG": 0.01},
                "max_sector_heat_pct": 0.015, "min_viable_risk_fraction": 0.5},
    "limits": {"max_trades_per_day_default": 5, "adv_participation_max": 0.01,
               "spread_max_bps": 40, "entry_blackout_final_min": 15},
    "model": {"backend": "stub", "retries_on_invalid": 1},
}
PROFILES = {
    "profiles": {
        "short_term_v1": {
            "initial_stop": {"method": "atr", "k": 2.0},
            "catastrophe": {"method": "atr", "k": 3.5},
            "breakeven_at_R": 1.0,
            "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
            "time_stop": {"window": "thesis", "min_progress_R": 0.5},
            "realization": {"target_fraction": 0.7, "action": "scale_out_50"},
            "earnings_blackout_exit": True, "overnight_hold": "eod_rule_v1"},
        "long_term_v1": {
            "initial_stop": {"method": "atr", "k": 3.0},
            "catastrophe": {"method": "atr", "k": 4.5},
            "breakeven_at_R": 1.0,
            "trail": {"activate_at_R": 2.0, "method": "atr_weekly", "k": 4.0},
            "time_stop": None,
            "realization": {"target_fraction": 0.7, "action": "review_flag"},
            "earnings_blackout_exit": False, "overnight_hold": "default_hold"}},
    "discretion_bands": {"k": [1.5, 2.5], "realization_fraction": [0.5, 0.9],
                         "time_window_sessions": [1, 3]},
}
DEADMAN_CFG = {"c4": {"reconcile_interval_min": 15,
                      "exit_unprotected_max_secs": 45,
                      "drawdown_breaker_pct": 0.02, "heartbeat_secs": 60}}

ADJ_OK = json.dumps({"k": 2.0, "realization_fraction": 0.7,
                     "time_window_sessions": 2, "reason": "clean confirm"})


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     journal.regime_snapshots, journal.intents, journal.orders,
                     journal.fills, journal.positions, journal.position_events,
                     journal.exits, journal.audit,
                     news.cluster_members, news.clusters, news.news_items,
                     queue.messages
                     RESTART IDENTITY CASCADE""")
        await c.execute("DELETE FROM journal.control")
    await register_config_version("phase4 integration test")
    await ensure_defaults()
    await set_flag("trading_capital", "50000", "TEST")
    # seed a thesis decision for the positions FK + an item row
    async with pool.connection() as c:
        await c.execute(
            """INSERT INTO news.news_items (item_id, revision, source,
                 source_tier, headline, content_hash, symbols, channels,
                 published_ts, received_ts)
               VALUES ('alpaca:9001',1,'alpaca_benzinga',2,'Acme wins contract',
                       'h9001','{ACME}','{}',%s,%s)""", (PIN, PIN))
        cur = await c.execute(
            """INSERT INTO journal.decisions (signal_id, item_id, stage, agent,
                 action, ticker, reason, config_version)
               SELECT 'alpaca:9001','alpaca:9001','ANALYST','A2','THESIS',
                      'ACME','test thesis', config_version
               FROM journal.config_versions LIMIT 1
               RETURNING decision_id""")
        thesis_id = (await cur.fetchone())[0]
    return {"pool": pool, "thesis_id": thesis_id}


async def seed_thesis(env, signal_id, ticker):
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO journal.decisions (signal_id, stage, agent, action,
                 ticker, reason, config_version)
               SELECT %s,'ANALYST','A2','THESIS',%s,'test thesis',
                      config_version FROM journal.config_versions LIMIT 1""",
            (signal_id, ticker))


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args)
        return await cur.fetchall()


def gatepass_msg(signal_id="alpaca:9001", ticker="ACME", atr=2.0,
                 magnitude=0.055, horizon="SHORT"):
    return {"envelope": {"msg_schema": "signal.risk/1", "producer": "C3",
                         "trace": {"signal_id": signal_id, "item_id": signal_id,
                                   "revision": 1}},
            "body": {"item_ref": {"item_id": signal_id, "revision": 1},
                     "regime_id": None,
                     "thesis": {"ticker": ticker, "direction": "up",
                                "magnitude_est": magnitude,
                                "expected_move_window": "2_sessions",
                                "horizon": horizon, "confidence": 0.7,
                                "priced_in_assessment": "x",
                                "source_risk": "low",
                                "invalidation": {
                                    "machine_checkable": ["close_below_prenews"],
                                    "news_checkable": ["denial"]},
                                "related_opportunities": [], "reason": "r"},
                     "gate": {"verdict": "PASS", "rule": "intraday",
                              "pct_move": 0.02, "vol_mult": 3.0, "minutes": 10,
                              "snapshot": {"ref_price": 100.0, "bid": 99.98,
                                           "ask": 100.02, "spread_bps": 4.0,
                                           "adv_20d": 5_000_000,
                                           "atr_14": atr,
                                           "ts": PIN.isoformat()}}}}


def a3(backend_replies=None):
    return A3Service(RISK_CFG, PROFILES,
                     backend=StubBackend(backend_replies or [ADJ_OK]),
                     now_fn=lambda: PIN)


def c4(broker):
    return C4Service(DEADMAN_CFG, broker=broker, now_fn=lambda: PIN,
                     poll_sleep=0.01, fill_timeout_secs=0.05)


async def process(queue_name, key, payload, handler):
    await enqueue(queue_name, key, payload)
    msg = await claim(queue_name, "test")
    assert msg is not None
    await handler(msg)
    await ack(msg.msg_id)
    return msg


# ---------------------------------------------------------------------------

async def test_01_reconcile_seeds_capital_rows(env):
    broker = FakeBroker(equity=48_000, settled_cash=48_000)
    await reconcile(broker)
    rows = await q(env, "SELECT key, value FROM journal.control "
                        "WHERE key IN ('broker_equity','settled_cash')")
    vals = dict(rows)
    assert float(vals["broker_equity"]) == 48_000
    assert float(vals["settled_cash"]) == 48_000


async def test_02_a3_sizes_and_emits_intent(env):
    svc = a3()
    await process("signal.risk", "alpaca:9001:1", gatepass_msg(), svc.handle)

    rows = await q(env, """SELECT action, payload->'sizing'->>'qty',
                                  payload->>'intent_id',
                                  payload->'sizing'->'clips' IS NOT NULL
                           FROM journal.decisions
                           WHERE stage='RISK' AND signal_id='alpaca:9001'""")
    action, qty, intent_id, has_clips = rows[0]
    # effective capital = min(48000 broker, 50000 config) = 48000
    # risk 240 / stop 4.0 = 60 shares
    assert (action, qty) == ("SIZE", "60") and has_clips

    rows = await q(env, """SELECT qty, limit_price, status, horizon,
                                  effective_capital
                           FROM journal.intents WHERE intent_id=%s""", intent_id)
    qty_i, limit, status, horizon, eff = rows[0]
    assert (qty_i, status, horizon) == (60, "PENDING", "SHORT")
    assert float(eff) == 48_000

    rows = await q(env, """SELECT payload->'body'->>'intent_id'
                           FROM queue.messages
                           WHERE queue_name='exec.intent' AND dedup_key=%s""",
                   intent_id)
    assert rows[0][0] == intent_id



async def test_03_missing_thesis_lineage_vetoes(env):
    """A GatePass whose ANALYST decision can't be found must not size —
    positions.thesis_decision_id is NOT NULL by design."""
    svc = a3()
    await process("signal.risk", "orphan:1:1",
                  gatepass_msg(signal_id="orphan:1", ticker="ORFN"), svc.handle)
    rows = await q(env, """SELECT action, veto_reason FROM journal.decisions
                           WHERE stage='RISK' AND signal_id='orphan:1'""")
    assert tuple(rows[0]) == ("VETO", "NO_THESIS_LINEAGE")
    rows = await q(env, """SELECT count(*) FROM queue.messages
                           WHERE queue_name='exec.intent'
                             AND payload->'envelope'->'trace'->>'signal_id'='orphan:1'""")
    assert rows[0][0] == 0


async def test_04_c4_fills_and_arms_two_tier_stops(env):
    broker = FakeBroker(equity=48_000, settled_cash=48_000)
    await reconcile(broker)
    svc = c4(broker)
    msg = await claim("exec.intent", "test-c4")
    assert msg is not None
    intent_id = msg.payload["body"]["intent_id"]
    await svc.handle_intent(msg)
    await ack(msg.msg_id)

    rows = await q(env, """SELECT p.ticker, p.qty_open, p.avg_entry,
                                  p.initial_stop, p.r_unit,
                                  p.exit_policy->'catastrophe_stop_broker'->>'price',
                                  p.catastrophe_stop_order_id
                           FROM journal.positions p
                           WHERE p.entry_intent_id=%s""", intent_id)
    ticker, qty, avg_entry, stop, r_unit, cat_price, cat_order = rows[0]
    assert (ticker, qty) == ("ACME", 60)
    fill = float(avg_entry)
    assert float(stop) == pytest.approx(fill - 4.0, abs=0.01)      # k=2 x atr=2
    assert float(cat_price) == pytest.approx(fill - 7.0, abs=0.01) # k=3.5
    assert float(r_unit) == pytest.approx(4.0, abs=0.01)
    assert cat_order is not None

    rows = await q(env, """SELECT order_role, state FROM journal.orders
                           ORDER BY order_id""")
    roles = {r[0]: r[1] for r in rows}
    assert roles["ENTRY"] == "FILLED" and roles["CATASTROPHE_STOP"] == "ACCEPTED"

    rows = await q(env, """SELECT event_type FROM journal.position_events""")
    assert ("STOPS_PLACED",) in rows

    # broker got exactly one entry + one stop, stop at the re-materialized price
    assert len(broker.submissions) == 2
    assert broker.submissions[1]["stop"] == pytest.approx(fill - 7.0, abs=0.01)

    rows = await q(env, "SELECT status FROM journal.intents WHERE intent_id=%s",
                   intent_id)
    assert rows[0][0] == "FILLED"


async def test_05_duplicate_intent_replay_is_noop(env):
    """Crash-replay: same exec.intent message again -> no second order."""
    broker = FakeBroker(equity=48_000, settled_cash=48_000)
    svc = c4(broker)
    rows = await q(env, "SELECT intent_id FROM journal.intents WHERE status='FILLED' LIMIT 1")
    intent_id = rows[0][0]
    body = {"intent_id": intent_id, "ticker": "ACME", "side": "BUY", "qty": 60,
            "limit_price": 100.04, "exit_policy": {}, "horizon": "SHORT",
            "thesis_decision_id": env["thesis_id"], "gate_snapshot": {}}
    await process("exec.intent", f"replay:{intent_id}",
                  {"envelope": {"msg_schema": "exec.intent/1", "producer": "A3",
                                "trace": {"signal_id": "alpaca:9001"}},
                   "body": body}, svc.handle_intent)
    assert broker.submissions == []                 # nothing hit the broker
    rows = await q(env, "SELECT count(*) FROM journal.positions WHERE ticker='ACME'")
    assert rows[0][0] == 1                          # still one position


async def test_05b_discretion_fallback_on_invalid_output(env):
    """Model returns out-of-band k twice -> profile defaults, trade proceeds."""
    async with env["pool"].connection() as c:
        await c.execute(
            """INSERT INTO journal.decisions (signal_id, stage, agent, action,
                 ticker, reason, config_version)
               SELECT 'syn:9002','ANALYST','A2','THESIS','ACME','syn thesis',
                      config_version FROM journal.config_versions LIMIT 1""")
    bad = json.dumps({"k": 9.0, "realization_fraction": 0.7,
                      "time_window_sessions": 2, "reason": "yolo"})
    svc = a3([bad, bad])
    await process("signal.risk", "syn:9002:1",
                  gatepass_msg(signal_id="syn:9002"), svc.handle)
    rows = await q(env, """SELECT payload->>'model_used',
                                  payload->'adjustments'->>'k',
                                  payload->'adjustments'->>'reason'
                           FROM journal.decisions
                           WHERE stage='RISK' AND signal_id='syn:9002'""")
    model_used, k, reason = rows[0]
    assert model_used == "false" and k == "2.0" and reason.startswith("fallback")
    # drain the intent this run emitted so later tests claim their own
    msg = await claim("exec.intent", "test-drain")
    assert msg.payload["envelope"]["trace"]["signal_id"] == "syn:9002"
    await ack(msg.msg_id)

async def test_06_a3_heat_reflects_open_position(env):
    """Open ACME position (60 sh, 4.0 stop distance = $240 heat vs $960 lane
    cap) leaves headroom; a second signal sizes smaller via lane heat."""
    await seed_thesis(env, "alpaca:9003", "BETA")
    svc = a3()
    await process("signal.risk", "alpaca:9003:1",
                  gatepass_msg(signal_id="alpaca:9003", ticker="BETA"),
                  svc.handle)
    rows = await q(env, """SELECT action, payload->'sizing'->'clips'->>'lane_heat',
                                  payload->'sizing'->>'qty'
                           FROM journal.decisions
                           WHERE stage='RISK' AND signal_id='alpaca:9003'""")
    action, lane_clip, qty = rows[0]
    # lane cap 2% x 48000 = 960; used 240 -> headroom 720/4.0 = 180 shares
    assert action == "SIZE" and float(lane_clip) == pytest.approx(180.0)
    assert int(qty) == 60                            # raw 60 still under clips
    # drain the intent this run emitted so later tests claim their own
    msg = await claim("exec.intent", "test-drain")
    assert msg.payload["envelope"]["trace"]["signal_id"] == "alpaca:9003"
    await ack(msg.msg_id)


async def test_07_kill_switch_vetoes_at_a3(env):
    await seed_thesis(env, "alpaca:9004", "GAMA")
    await set_flag("kill_switch", "1", "TEST", "test kill")
    svc = a3()
    await process("signal.risk", "alpaca:9004:1",
                  gatepass_msg(signal_id="alpaca:9004", ticker="GAMA"),
                  svc.handle)
    rows = await q(env, """SELECT action, veto_reason FROM journal.decisions
                           WHERE stage='RISK' AND signal_id='alpaca:9004'""")
    assert tuple(rows[0]) == ("VETO", "KILL_SWITCH")
    await set_flag("kill_switch", "0", "TEST", "reset")


async def test_08_c4_preflight_blocks_when_flag_set(env):
    """A3 passed but the world changed before C4 submitted: block_entries."""
    await seed_thesis(env, "alpaca:9005", "DLTA")
    svc = a3()
    await process("signal.risk", "alpaca:9005:1",
                  gatepass_msg(signal_id="alpaca:9005", ticker="DLTA"),
                  svc.handle)
    await set_flag("block_entries", "1", "TEST", "deadman trip simulation")
    broker = FakeBroker(equity=48_000, settled_cash=48_000)
    svc4 = c4(broker)
    msg = await claim("exec.intent", "test-c4")
    await svc4.handle_intent(msg)
    await ack(msg.msg_id)
    rows = await q(env, """SELECT action, veto_reason FROM journal.decisions
                           WHERE stage='ORDER' AND ticker='DLTA'""")
    assert tuple(rows[0]) == ("VETO", "ENTRIES_BLOCKED")
    assert broker.submissions == []
    rows = await q(env, """SELECT status FROM journal.intents i
                           JOIN journal.decisions d ON d.decision_id=i.decision_id
                           WHERE d.signal_id='alpaca:9005'""")
    assert rows[0][0] == "REJECTED"
    await set_flag("block_entries", "0", "TEST", "reset")


async def test_09_unfilled_entry_expires(env):
    await seed_thesis(env, "alpaca:9006", "EPSN")
    svc = a3()
    await process("signal.risk", "alpaca:9006:1",
                  gatepass_msg(signal_id="alpaca:9006", ticker="EPSN"),
                  svc.handle)
    broker = FakeBroker(equity=48_000, settled_cash=48_000)
    broker.set_behavior("EPSN", "rest")             # never fills
    svc4 = c4(broker)
    msg = await claim("exec.intent", "test-c4")
    await svc4.handle_intent(msg)
    await ack(msg.msg_id)
    rows = await q(env, """SELECT status FROM journal.intents i
                           JOIN journal.decisions d ON d.decision_id=i.decision_id
                           WHERE d.signal_id='alpaca:9006' AND d.stage='RISK'""")
    assert rows[0][0] == "CANCELLED"                # timed out, cancelled
    rows = await q(env, "SELECT count(*) FROM journal.positions WHERE ticker='EPSN'")
    assert rows[0][0] == 0


async def test_10_reconciliation_drift(env):
    """Broker lost ACME (CLOSED_EXTERNAL) and holds mystery ZETA (ADOPTED)."""
    broker = FakeBroker(equity=47_000, settled_cash=41_000)
    broker.inject_position("ZETA", 25, 40.0)        # unknown locally
    # broker has NO ACME (dropped)
    summary = await reconcile(broker)
    assert summary["closed_external"] == ["ACME"]
    assert summary["adopted"] == ["ZETA"]

    rows = await q(env, "SELECT status FROM journal.positions WHERE ticker='ACME'")
    assert rows[0][0] == "CLOSED"
    rows = await q(env, """SELECT status, qty_open, profile
                           FROM journal.positions WHERE ticker='ZETA'""")
    status, qty, profile = rows[0]
    assert (status, qty, profile) == ("OPEN", 25, "adopted_v1")
    rows = await q(env, """SELECT count(*) FROM journal.audit
                           WHERE action IN ('RECONCILE_CLOSED_EXTERNAL',
                                            'RECONCILE_ADOPTED')""")
    assert rows[0][0] == 2
    vals = dict(await q(env, "SELECT key, value FROM journal.control "
                             "WHERE key IN ('broker_equity','settled_cash')"))
    assert float(vals["broker_equity"]) == 47_000


async def test_11_reconciliation_qty_snap(env):
    broker = FakeBroker(equity=47_000, settled_cash=41_000)
    broker.inject_position("ZETA", 20, 40.0)        # broker says 20, DB says 25
    summary = await reconcile(broker)
    assert summary["qty_snapped"] == ["ZETA"]
    rows = await q(env, "SELECT qty_open FROM journal.positions WHERE ticker='ZETA'")
    assert rows[0][0] == 20

```

## `tests/integration/test_triage_flow.py`

```python
"""Phase 2 integration: DedupedSignal in -> TRIAGE decision row + routed
TriagedSignal out, against real PostgreSQL 16 through the actual A1Service.

Covers: config_version registration; ESCALATE with market-open routing;
overnight routing with priority ordering; DISCARD journaling; thesis-lane for
untagged material items; guard fan-out on a held position (incl. the
immaterial-but-held case); REJECT after retry exhaustion (raw output
journaled); retry-then-success consuming exactly two model calls; retrieval
promotion on material=true; decision+routing atomicity via the shared tx.
"""
import json
import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ.setdefault("QDRANT_PATH", "/tmp/qdrant-test-p2")

from common.db import get_pool
from common.journal import register_config_version
from common.queue import claim, enqueue
from a1_triage.backends import StubBackend
from a1_triage.service import A1Service
from c2_dedup.vectorstore import VectorStore
from router import facts as facts_mod

pytestmark = pytest.mark.asyncio(loop_scope="session")

CFG = {
    "model": {"backend": "stub", "retries_on_invalid": 1},
    "router": {"tier_weight": {1: 6, 2: 4, 3: 1},
               "urgency_weight": {"high": 6, "medium": 3, "low": 0},
               "corroboration_bonus_per_outlet": 1, "corroboration_bonus_cap": 3,
               "overnight_base": 50},
}

OPEN_TS = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)     # Tue 11:00 ET
CLOSED_TS = datetime(2026, 7, 7, 1, 0, tzinfo=timezone.utc)    # Mon 21:00 ET


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    import shutil
    shutil.rmtree("/tmp/qdrant-test-p2", ignore_errors=True)
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE journal.decisions, journal.config_versions,
                     queue.messages RESTART IDENTITY CASCADE""")
    await register_config_version("phase2 integration test")
    yield {"pool": pool, "store": VectorStore(path="/tmp/qdrant-test-p2")}


def deduped(item_id: str, headline: str, *, revision=1, tier=2, symbols=None,
            outlets=1, new_story=True, summary="") -> dict:
    return {"envelope": {"msg_schema": "signal.dedup/1", "producer": "C2",
                         "trace": {"signal_id": item_id, "item_id": item_id,
                                   "revision": revision}},
            "body": {"item": {"item_id": item_id, "revision": revision,
                              "headline": headline, "summary": summary,
                              "source": "alpaca_benzinga", "source_tier": tier,
                              "symbols": symbols or [],
                              "published_ts": "2026-07-07T14:30:00.000Z"},
                     "cluster": {"cluster_id": 1, "is_new_story": new_story,
                                 "independent_outlets": outlets, "total_items": 1,
                                 "similarity_to_canonical": 1.0}}}


def stub_reply(material, tickers, urgency="high", novelty=0.9):
    return json.dumps({"material": material, "tickers": tickers,
                       "direction_hint": "up", "urgency": urgency,
                       "novelty_score": novelty, "reason": "scripted"})


async def run_one(env, payload: dict, scripted: list[str], now, key: str):
    svc = A1Service(CFG, backend=StubBackend(scripted), store=env["store"])
    # pin the clock for market_open determinism
    orig = facts_mod.market_open_now
    facts_mod.market_open_now = lambda _=None, _now=now: orig(_now)
    try:
        await enqueue("signal.triage", key, payload)
        msg = await claim("signal.triage", "test-a1")
        assert msg is not None and msg.dedup_key == key
        await svc.handle(msg)
        from common.queue import ack
        await ack(msg.msg_id)
        return svc
    finally:
        facts_mod.market_open_now = orig


async def q(env, sql, *args):
    async with env["pool"].connection() as c:
        cur = await c.execute(sql, args)
        return await cur.fetchall()


# --------------------------------------------------------------------------------

async def test_01_escalate_market_open_routes_analyst(env):
    await run_one(env, deduped("alpaca:5001", "Acme acquisition at $45", symbols=["ACME"]),
                  [stub_reply(True, ["ACME"])], OPEN_TS, "alpaca:5001:1")
    rows = await q(env, """SELECT action, ticker, model_id,
                                  payload->'routing'->>'market_open'
                           FROM journal.decisions WHERE item_id='alpaca:5001'""")
    assert rows[0] == ("ESCALATE", "ACME", "stub-0", "true")
    rows = await q(env, """SELECT payload->'body'->'triage'->>'material'
                           FROM queue.messages
                           WHERE queue_name='signal.analyst' AND dedup_key='alpaca:5001:1'""")
    assert rows[0][0] == "true"


async def test_02_market_closed_overnight_with_priority(env):
    await run_one(env, deduped("alpaca:5002", "Zenith FDA approval", tier=1, symbols=["ZNTH"]),
                  [stub_reply(True, ["ZNTH"], urgency="high", novelty=1.0)],
                  CLOSED_TS, "alpaca:5002:1")
    # score: tier1(6)+high(6)+round(1.0*4)=4+0 = 16 -> queue priority 34
    rows = await q(env, """SELECT priority FROM queue.messages
                           WHERE queue_name='signal.overnight' AND dedup_key='alpaca:5002:1'""")
    assert rows[0][0] == 34


async def test_03_discard_journaled_no_routes(env):
    await run_one(env, deduped("alpaca:5003", "TechWave wins innovation award"),
                  [stub_reply(False, [])], OPEN_TS, "alpaca:5003:1")
    rows = await q(env, "SELECT action FROM journal.decisions WHERE item_id='alpaca:5003'")
    assert rows[0][0] == "DISCARD"
    rows = await q(env, """SELECT count(*) FROM queue.messages
                           WHERE dedup_key='alpaca:5003:1'
                             AND queue_name != 'signal.triage'""")
    assert rows[0][0] == 0


async def test_04_material_no_ticker_thesis_lane(env):
    await run_one(env, deduped("edgar:acc-1", "8-K - MYSTERY CORP: FDA CRL received", tier=1),
                  [stub_reply(True, [])], OPEN_TS, "edgar:acc-1:1")
    rows = await q(env, """SELECT queue_name FROM queue.messages
                           WHERE dedup_key='edgar:acc-1:1' AND queue_name != 'signal.triage'""")
    assert [r[0] for r in rows] == ["signal.thesis"]      # never intraday, even market-open


async def test_05_guard_fanout_on_held_position(env):
    # Plant an open position with its full FK chain (decision -> intent ->
    # position), exactly as Phase 4's A3/C4 will create it.
    async with env["pool"].connection() as c:
        cur = await c.execute(
            """INSERT INTO journal.decisions
               (signal_id, ticker, stage, agent, action, payload, config_version)
               VALUES ('sig-fixture', 'ACME', 'RISK', 'A3', 'SIZED', '{}',
                       (SELECT config_version FROM journal.config_versions LIMIT 1))
               RETURNING decision_id""")
        dec_id = (await cur.fetchone())[0]
        await c.execute(
            """INSERT INTO journal.intents
               (intent_id, decision_id, ticker, side, qty, limit_price,
                horizon, status, config_version)
               VALUES ('int-fixture-1', %s, 'ACME', 'BUY', 50, 100.20,
                       'SHORT', 'FILLED',
                       (SELECT config_version FROM journal.config_versions LIMIT 1))""",
            (dec_id,))
        await c.execute(
            """INSERT INTO journal.positions
               (ticker, horizon, profile, status, opened_ts, entry_intent_id,
                thesis_decision_id, qty_initial, qty_open, avg_entry,
                initial_stop, r_unit, exit_policy, config_version)
               VALUES ('ACME', 'SHORT', 'default', 'OPEN', now(),
                       'int-fixture-1', %s, 50, 50, 100.00, 95.00, 5.00, '{}',
                       (SELECT config_version FROM journal.config_versions LIMIT 1))""",
            (dec_id,))
    await run_one(env, deduped("alpaca:5005", "Acme guidance cut", revision=2, symbols=["ACME"]),
                  [stub_reply(True, ["ACME"])], OPEN_TS, "alpaca:5005:2")
    rows = await q(env, """SELECT queue_name, priority FROM queue.messages
                           WHERE dedup_key='alpaca:5005:2' AND queue_name != 'signal.triage'
                           ORDER BY queue_name""")
    assert ("signal.guard", 0) in [tuple(r) for r in rows]
    assert ("signal.analyst", 100) in [tuple(r) for r in rows]


async def test_06_immaterial_but_held_still_guards(env):
    await run_one(env, deduped("alpaca:5006", "Acme sponsors charity golf event", symbols=["ACME"]),
                  [stub_reply(False, ["ACME"])], OPEN_TS, "alpaca:5006:1")
    rows = await q(env, "SELECT action FROM journal.decisions WHERE item_id='alpaca:5006'")
    assert rows[0][0] == "DISCARD"
    rows = await q(env, """SELECT queue_name FROM queue.messages
                           WHERE dedup_key='alpaca:5006:1' AND queue_name != 'signal.triage'""")
    assert [r[0] for r in rows] == ["signal.guard"]


async def test_07_reject_after_retry_exhaustion(env):
    bad = "the item is clearly material because"
    await run_one(env, deduped("alpaca:5007", "Acme merger talk"),
                  [bad, bad], OPEN_TS, "alpaca:5007:1")
    rows = await q(env, """SELECT action, payload->>'raw_output', payload->>'attempts'
                           FROM journal.decisions WHERE item_id='alpaca:5007'""")
    action, raw, attempts = rows[0]
    assert action == "REJECT" and raw.startswith("the item") and attempts == "2"
    rows = await q(env, """SELECT count(*) FROM queue.messages
                           WHERE dedup_key='alpaca:5007:1' AND queue_name != 'signal.triage'""")
    assert rows[0][0] == 0                        # rejected items route nowhere


async def test_08_retry_then_success_two_calls(env):
    svc = await run_one(env, deduped("alpaca:5008", "Acme buyback", symbols=["ACME"]),
                        ["not json at all", stub_reply(True, ["ACME"])],
                        OPEN_TS, "alpaca:5008:1")
    assert len(svc.backend.calls) == 2
    assert "previous response was invalid" in svc.backend.calls[1][-1]["content"]
    rows = await q(env, "SELECT action FROM journal.decisions WHERE item_id='alpaca:5008'")
    assert rows[0][0] == "ESCALATE"


async def test_09_retrieval_promotion_material_only(env):
    store = env["store"]
    n = store.client.count(store.retrieval).count
    # material items promoted so far: 5001, 5002, acc-1, 5005, 5008 = 5
    assert n == 5, f"expected 5 promoted items, got {n}"
    # a discarded item must NOT be in retrieval
    hits = store.client.scroll(store.retrieval, limit=50)[0]
    ids = {h.payload["item_id"] for h in hits}
    assert "alpaca:5003" not in ids and "alpaca:5006" not in ids


async def test_10_config_version_stamped(env):
    rows = await q(env, """SELECT DISTINCT config_version FROM journal.decisions""")
    assert len(rows) == 1
    rows2 = await q(env, """SELECT count(*) FROM journal.config_versions
                            WHERE config_version = %s""", rows[0][0])
    assert rows2[0][0] == 1

```

## `tests/unit/__init__.py`

```python

```

## `tests/unit/test_analyst_gate.py`

```python
"""Phase 3 unit tests: ThesisOutput incl. DSL-validated invalidations, the C3
rules matrix, credibility matrix, market-data indicators, C8 features. No DB."""
import asyncio
import json

import pytest

from a2_analyst.schema import (ThesisValidationError, thesis_json_schema,
                               validate_thesis)
from c3_gate.rules import GateVerdict, MarketState, credibility_required, evaluate
from common.marketdata import FakeData, adv20, atr14, realized_vol, sma

GATE_CFG = {
    "intraday_move_pct": 0.015, "intraday_vol_mult": 2.5,
    "intraday_window_min": 30, "extended_pct": 0.06,
    "open_blackout_min": 15, "handoff_gap_ratio": 0.5,
    "impact_medium_min": 0.02, "impact_high_min": 0.05,
    "required_outlets": {"low": {2: 1, 3: 1}, "medium": {2: 1, 3: 2},
                         "high": {2: 2, 3: 3}},
}


def thesis_json(**over):
    base = {"ticker": "ACME", "direction": "up", "magnitude_est": 0.055,
            "expected_move_window": "2_sessions", "horizon": "SHORT",
            "confidence": 0.72, "priced_in_assessment": "moved 1.1% of 5.5% est",
            "source_risk": "low",
            "invalidation": {"machine_checkable": ["close_below_prenews"],
                             "news_checkable": ["counterparty denial"]},
            "related_opportunities": [], "reason": "supply repricing"}
    base.update(over)
    return json.dumps(base)


# ---- thesis schema + DSL hook -------------------------------------------------

def test_valid_thesis_parses():
    t = validate_thesis(thesis_json())
    assert t.ticker == "ACME" and t.invalidation.machine_checkable == ["close_below_prenews"]


def test_unknown_stdlib_predicate_rejected():
    with pytest.raises(ThesisValidationError) as e:
        validate_thesis(thesis_json(
            invalidation={"machine_checkable": ["price_goes_down_a_lot"],
                          "news_checkable": []}))
    assert "unknown stdlib predicate" in e.value.detail


def test_full_mip_spec_accepted():
    spec = {"id": "custom_1",
            "when": {"metric": "close", "tf": "session", "op": "<",
                     "value": {"ref": "prenews_price"}},
            "persist": {"bars": 1}, "action": {"type": "exit"}}
    t = validate_thesis(thesis_json(
        invalidation={"machine_checkable": [spec], "news_checkable": []}))
    assert t.invalidation.machine_checkable[0]["id"] == "custom_1"


def test_invalid_mip_spec_rejected():
    bad = {"id": "x", "when": {"metric": "vibes", "tf": "1m", "op": "<", "value": 1},
           "persist": {"bars": 1}, "action": {"type": "exit"}}
    with pytest.raises(ThesisValidationError) as e:
        validate_thesis(thesis_json(
            invalidation={"machine_checkable": [bad], "news_checkable": []}))
    assert "MIP spec invalid" in e.value.detail


def test_move_window_pattern():
    with pytest.raises(ThesisValidationError):
        validate_thesis(thesis_json(expected_move_window="soon"))
    assert validate_thesis(thesis_json(expected_move_window="3_weeks"))


def test_magnitude_bounds():
    with pytest.raises(ThesisValidationError):
        validate_thesis(thesis_json(magnitude_est=0.9))    # > 50% is a hallucination


def test_thesis_schema_generates():
    s = thesis_json_schema()
    assert "invalidation" in s["required"]


# ---- credibility matrix ----------------------------------------------------------

def test_tier1_passes_alone():
    assert credibility_required("high", 1, "high", GATE_CFG) == 1


def test_tier3_high_impact_never_alone():
    assert credibility_required("high", 3, "low", GATE_CFG) == 3


def test_source_risk_bumps_level():
    # medium impact tier-3 normally 2; high source_risk -> treated as high -> 3
    assert credibility_required("medium", 3, "low", GATE_CFG) == 2
    assert credibility_required("medium", 3, "high", GATE_CFG) == 3


# ---- gate rules matrix -------------------------------------------------------------

def state(**over):
    base = dict(prenews_price=100.0, last_price=102.0, vol_mult=3.0,
                minutes_since_publish=10, news_in_session=True,
                minutes_since_open=120, gap_pct=None,
                corroboration_outlets=2, tier_min=2)
    base.update(over)
    return MarketState(**base)


def thesis_d(**over):
    base = {"ticker": "ACME", "direction": "up", "magnitude_est": 0.055,
            "source_risk": "low"}
    base.update(over)
    return base


def test_intraday_pass():
    v = evaluate(thesis_d(), state(), GATE_CFG)
    assert (v.verdict, v.rule, v.veto_reason) == ("PASS", "intraday", None)
    assert v.numbers["pct_move"] == 0.02


def test_long_only_veto():
    v = evaluate(thesis_d(direction="down"), state(), GATE_CFG)
    assert v.veto_reason == "LONG_ONLY"


def test_credibility_veto_tier3_single_source():
    v = evaluate(thesis_d(), state(corroboration_outlets=1, tier_min=3), GATE_CFG)
    assert v.veto_reason == "CREDIBILITY"
    assert v.numbers["credibility"]["required_outlets"] == 3


def test_window_veto():
    v = evaluate(thesis_d(), state(minutes_since_publish=45), GATE_CFG)
    assert v.veto_reason == "GATE_WINDOW"


def test_extended_veto():
    v = evaluate(thesis_d(), state(last_price=107.0), GATE_CFG)
    assert v.veto_reason == "GATE_EXTENDED"


def test_no_confirm_low_volume():
    v = evaluate(thesis_d(), state(vol_mult=1.2), GATE_CFG)
    assert v.veto_reason == "GATE_NO_CONFIRM"


def test_no_confirm_small_move():
    v = evaluate(thesis_d(), state(last_price=100.5), GATE_CFG)
    assert v.veto_reason == "GATE_NO_CONFIRM"


def test_handoff_open_blackout():
    v = evaluate(thesis_d(), state(news_in_session=False, minutes_since_open=5,
                                   gap_pct=0.01), GATE_CFG)
    assert (v.rule, v.veto_reason) == ("open_handoff", "GATE_OPEN_WINDOW")


def test_handoff_priced_in_large_gap():
    # gap 3% vs est 5.5% -> ratio 0.545 >= 0.5 -> priced in
    v = evaluate(thesis_d(), state(news_in_session=False, minutes_since_open=30,
                                   gap_pct=0.03), GATE_CFG)
    assert v.veto_reason == "PRICED_IN"


def test_handoff_small_gap_passes():
    v = evaluate(thesis_d(), state(news_in_session=False, minutes_since_open=30,
                                   gap_pct=0.01, last_price=101.0), GATE_CFG)
    assert v.verdict == "PASS" and v.rule == "open_handoff"


# ---- indicators + C8 features -----------------------------------------------------------

def test_atr14_needs_15_bars():
    assert atr14(FakeData.flat_daily(10)) is None
    assert atr14(FakeData.flat_daily(20)) == pytest.approx(1.0, rel=0.05)


def test_adv20():
    assert adv20(FakeData.flat_daily(25, volume=2_000_000)) == 2_000_000


def test_realized_vol_flat_is_zero():
    assert realized_vol(FakeData.flat_daily(30)) == pytest.approx(0.0, abs=1e-6)


def test_c8_features_shapes():
    from c8_regime.service import compute_features
    md = FakeData()
    feats = asyncio.run(compute_features(md))
    assert feats["index_trend"] in ("above_50d", "below_50d")
    assert 0.0 <= feats["breadth_proxy"] <= 1.0
    assert "realized_vol_20d" in feats
    assert "vix" not in feats                       # honest naming: proxy, not VIX
    assert feats["source"] == "etf_proxies_iex"
    assert len(feats["sector_rs"]["top"]) == 3

```

## `tests/unit/test_exit_engine.py`

```python
"""Phase 4 chunk-2 unit tests: exit evaluator (layer priority, stop
attribution, tighten-only ratchets, HWM, scale-out), D1 overnight matrix.
Pure functions, no DB."""
import pytest

from c4_exec.exits import (ExitAction, evaluate_on_bar, policy_state,
                           realization_target)
from c4_exec.overnight import overnight_decision, realized_move_fraction

ON_CFG = {"hold_min_unrealized_R": 0.3, "young_max_age_sessions": 1,
          "young_max_realized_fraction": 0.5}


def make_pos(**over):
    policy = {
        "profile": "short_term_v1",
        "initial_stop": {"method": "atr", "k": 2.0, "price": 96.0},
        "catastrophe_stop_broker": {"k": 3.5, "price": 93.0},
        "breakeven_at_R": 1.0,
        "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
        "time_stop": {"window": "2_sessions", "min_progress_R": 0.5},
        "realization": {"target_fraction": 0.7, "action": "scale_out_50"},
        "magnitude_est": 0.055,
        "atr_14": 2.0,
    }
    policy.update(over.pop("policy", {}))
    pos = {"position_id": 1, "ticker": "ACME", "horizon": "SHORT",
           "qty_open": 60, "avg_entry": 100.0, "r_unit": 4.0,
           "exit_policy": policy, "opened_ts": None, "last_price": None}
    pos.update(over)
    return pos


def bar(o=100.0, h=100.5, l=99.5, c=100.0):
    return {"ts": 1751900000, "open": o, "high": h, "low": l, "close": c}


# ---- L1 attribution ----------------------------------------------------------

def test_l1_initial_stop_attribution():
    a = evaluate_on_bar(make_pos(), bar(l=95.9, c=96.5), 0)
    assert len(a) == 1 and (a[0].kind, a[0].layer, a[0].qty) == ("EXIT", "STOP", 60)


def test_l1_breakeven_attribution():
    pos = make_pos(policy={"current_stop": 100.0, "stop_basis": "breakeven"})
    a = evaluate_on_bar(pos, bar(l=99.9, c=100.2), 0)
    assert (a[0].kind, a[0].layer) == ("EXIT", "BREAKEVEN")


def test_l1_trail_attribution():
    pos = make_pos(policy={"current_stop": 103.0, "stop_basis": "trail",
                           "hwm": 108.0})
    a = evaluate_on_bar(pos, bar(o=104, h=104, l=102.8, c=103.5), 0)
    assert (a[0].kind, a[0].layer) == ("EXIT", "TRAIL")


def test_l1_beats_l4_same_bar():
    """A bar that touches both the stop and the target: the stop wins —
    conservative attribution, full exit."""
    a = evaluate_on_bar(make_pos(), bar(h=110.0, l=95.0, c=100.0), 0)
    assert len(a) == 1 and a[0].layer == "STOP"


# ---- L5 invalidation -----------------------------------------------------------

class FakeFire:
    predicate_id = "close_below_prenews"
    detail = "persisted 2 bars"
    action = {"type": "exit"}


def test_l5_invalidation_full_exit():
    a = evaluate_on_bar(make_pos(), bar(), 0, [FakeFire()])
    assert (a[0].kind, a[0].layer, a[0].qty) == ("EXIT", "INVALIDATION", 60)


def test_l1_beats_l5_same_bar():
    a = evaluate_on_bar(make_pos(), bar(l=95.0), 0, [FakeFire()])
    assert a[0].layer == "STOP"


# ---- L3 time stop -----------------------------------------------------------------

def test_l3_time_stop_fires_when_stale():
    # age 2 sessions >= 2 window, progress 0.25R < 0.5R
    a = evaluate_on_bar(make_pos(), bar(c=101.0), 2)
    assert (a[0].kind, a[0].layer) == ("EXIT", "TIME")


def test_l3_holds_when_progressing():
    # progress 0.75R >= 0.5R min
    a = evaluate_on_bar(make_pos(), bar(h=103.2, c=103.0), 2)
    assert all(x.layer != "TIME" for x in a)


def test_l3_absent_for_long_profile():
    pos = make_pos(policy={"time_stop": None})
    a = evaluate_on_bar(pos, bar(c=100.5), 5)
    assert all(x.layer != "TIME" for x in a)


# ---- L4 realization ------------------------------------------------------------------

def test_l4_target_price():
    # 100 * (1 + 0.7*0.055) = 103.85
    assert realization_target(100.0, make_pos()["exit_policy"]) == 103.85


def test_l4_scale_out_half():
    a = evaluate_on_bar(make_pos(), bar(h=104.0, c=103.9), 0)
    scale = [x for x in a if x.kind == "SCALE_OUT"]
    assert scale and (scale[0].layer, scale[0].qty) == ("TARGET", 30)


def test_l4_only_once():
    pos = make_pos(policy={"scale_out_done": True})
    a = evaluate_on_bar(pos, bar(h=105.0, c=104.5), 0)
    assert not [x for x in a if x.kind == "SCALE_OUT"]


def test_l4_review_flag_for_long():
    pos = make_pos(policy={"realization": {"target_fraction": 0.7,
                                           "action": "review_flag"},
                           "time_stop": None})
    a = evaluate_on_bar(pos, bar(h=104.0, c=103.9), 0)
    ev = [x for x in a if x.kind == "EVENT" and x.event_type == "POSITION_REVIEW"]
    assert len(ev) == 1


# ---- L2 ratchets ---------------------------------------------------------------------

def test_l2_breakeven_moves_at_1R():
    a = evaluate_on_bar(make_pos(), bar(h=104.1, c=104.0), 0)   # +1.0R
    sets = [x for x in a if x.kind == "SET_STOP"]
    assert sets and (sets[0].new_stop, sets[0].new_basis) == (100.0, "breakeven")


def test_l2_no_breakeven_below_1R():
    a = evaluate_on_bar(make_pos(), bar(h=103.0, c=102.0), 0)   # +0.5R
    assert not [x for x in a if x.kind == "SET_STOP"]


def test_l2_trail_from_1_5R():
    # close 106 = +1.5R, hwm 106.5 -> trail = 106.5 - 2.5*2.0 = 101.5
    a = evaluate_on_bar(make_pos(), bar(h=106.5, c=106.0), 0)
    sets = [x for x in a if x.kind == "SET_STOP"]
    assert sets and (sets[0].new_stop, sets[0].new_basis) == (101.5, "trail")


def test_l2_tighten_only_never_loosens():
    # trail proposal 101.5 but current stop already 102 -> discarded
    pos = make_pos(policy={"current_stop": 102.0, "stop_basis": "trail",
                           "hwm": 106.5})
    a = evaluate_on_bar(pos, bar(h=106.5, c=106.0), 0)
    assert not [x for x in a if x.kind == "SET_STOP"]


def test_l2_trail_ratchets_with_new_high():
    pos = make_pos(policy={"current_stop": 101.5, "stop_basis": "trail",
                           "hwm": 106.5})
    a = evaluate_on_bar(pos, bar(h=109.0, l=105.0, c=108.5), 0)  # new hwm 109
    sets = [x for x in a if x.kind == "SET_STOP"]
    assert sets and sets[0].new_stop == 104.0            # 109 - 5.0


def test_hwm_tracked_without_ratchet():
    a = evaluate_on_bar(make_pos(), bar(h=102.0, c=101.0), 0)  # below 1R
    hwm_updates = [x for x in a if x.kind == "EVENT" and x.new_hwm == 102.0]
    assert len(hwm_updates) == 1


# ---- D1 overnight matrix ---------------------------------------------------------------

@pytest.mark.parametrize("unreal_r,age,frac,earn,expect,rule", [
    (2.0, 0, 0.9, True,  "EXIT", "earnings_next_session"),   # earnings trumps
    (0.5, 3, 0.2, False, "HOLD", "unrealized_R_threshold"),
    (0.3, 3, 0.2, None,  "HOLD", "unrealized_R_threshold"),  # boundary >=
    (0.1, 0, 0.3, None,  "HOLD", "young_position"),
    (0.1, 0, 0.6, None,  "EXIT", "stale_flat"),   # young but move captured
    (0.1, 1, 0.3, None,  "EXIT", "stale_flat"),   # not young anymore
    (-0.2, 2, -0.1, None, "EXIT", "stale_flat"),
])
def test_overnight_matrix(unreal_r, age, frac, earn, expect, rule):
    assert overnight_decision(unreal_r, age, frac, earn, ON_CFG) == (expect, rule)


def test_realized_move_fraction():
    assert realized_move_fraction(102.75, 100.0, 0.055) == pytest.approx(0.5)
    assert realized_move_fraction(100.0, 100.0, 0.0) == 0.0

```

## `tests/unit/test_normalize.py`

```python
"""Unit tests: clock discipline, content hash, source normalization.
No database required."""
import pytest
from datetime import datetime, timezone

from common.clock import iso_utc, parse_ts, is_market_hours
from common.contracts import NewsItem, content_hash
from c1_ingestion.normalize import (NormalizeError, normalize_alpaca,
                                    normalize_edgar, normalize_rss)


# ---- clock -------------------------------------------------------------------

def test_parse_ts_iso_z():
    dt = parse_ts("2026-07-07T14:32:11.481Z")
    assert dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0


def test_parse_ts_offset_converts_to_utc():
    dt = parse_ts("2026-07-07T10:32:11-04:00")
    assert dt.hour == 14


def test_parse_ts_rejects_naive():
    with pytest.raises(ValueError):
        parse_ts("2026-07-07T14:32:11")


def test_parse_ts_rejects_garbage():
    for bad in ("", "0000-00-00", "not a date", None):
        with pytest.raises((ValueError, TypeError)):
            parse_ts(bad)


def test_parse_ts_epoch_millis():
    dt = parse_ts(1751898731481)
    assert dt.year == 2025 or dt.year == 2026  # sanity: in-range epoch


def test_iso_utc_format():
    s = iso_utc(datetime(2026, 7, 7, 14, 32, 11, 481000, tzinfo=timezone.utc))
    assert s == "2026-07-07T14:32:11.481Z"


def test_market_hours_weekend_false():
    # Sunday noon ET
    assert not is_market_hours(datetime(2026, 7, 5, 16, 0, tzinfo=timezone.utc))


def test_market_hours_tuesday_true():
    # Tuesday 2026-07-07 14:00 UTC = 10:00 ET
    assert is_market_hours(datetime(2026, 7, 7, 14, 0, tzinfo=timezone.utc))


# ---- content hash --------------------------------------------------------------

def test_hash_ignores_trivial_reformatting():
    a = content_hash("Acme  Corp  Rises", "on   strong earnings")
    b = content_hash("acme corp rises", "on strong earnings")
    assert a == b


def test_hash_changes_on_real_edit():
    assert content_hash("Acme rises 5%") != content_hash("Acme rises 15%")


# ---- alpaca ---------------------------------------------------------------------

ALPACA_OK = {
    "T": "n", "id": 40892639, "headline": "Acme Corp Announces Buyback",
    "summary": "Board approves $2B repurchase.", "author": "B. Writer",
    "created_at": "2026-07-07T14:30:00Z", "updated_at": "2026-07-07T14:30:00Z",
    "symbols": ["acme"], "url": "https://example.com/x", "source": "benzinga",
}


def test_alpaca_ok():
    item = normalize_alpaca(ALPACA_OK)
    assert item.item_id == "alpaca:40892639"
    assert item.source_tier == 2
    assert item.symbols == ["ACME"]              # uppercased
    assert item.published_ts.tzinfo is not None
    assert item.received_ts >= item.published_ts


def test_alpaca_missing_headline_quarantines():
    bad = {**ALPACA_OK, "headline": ""}
    with pytest.raises(NormalizeError) as e:
        normalize_alpaca(bad)
    assert e.value.reason_code == "MISSING_REQUIRED_FIELD"


def test_alpaca_bad_timestamp_quarantines():
    bad = {**ALPACA_OK, "created_at": "0000-00-00"}
    with pytest.raises(NormalizeError) as e:
        normalize_alpaca(bad)
    assert e.value.reason_code == "BAD_TIMESTAMP"


def test_alpaca_symbols_not_list_quarantines():
    bad = {**ALPACA_OK, "symbols": "ACME"}
    with pytest.raises(NormalizeError) as e:
        normalize_alpaca(bad)
    assert e.value.reason_code == "UNKNOWN_SCHEMA"


def test_alpaca_empty_symbols_valid():
    """v0.2: symbols MAY BE EMPTY — untagged items are valid."""
    ok = {**ALPACA_OK, "symbols": []}
    assert normalize_alpaca(ok).symbols == []


# ---- edgar ------------------------------------------------------------------------

EDGAR_OK = {
    "id": "urn:tag:sec.gov,2008:accession-number=0001234567-26-000123",
    "title": "8-K - ACME CORP (0001234567) (Filer)",
    "link": "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000123-index.htm",
    "updated": "2026-07-07T16:45:00-04:00",
    "summary": "Item 2.02 Results of Operations",
}


def test_edgar_ok_8k_channel():
    item = normalize_edgar(EDGAR_OK)
    assert item.item_id == "edgar:0001234567-26-000123"
    assert item.source_tier == 1
    assert "8-K" in item.channels
    assert item.symbols == []                    # CIK != ticker; mapping is not C1's job


def test_edgar_friday_pm_flag():
    # 2026-07-10 is a Friday; 16:45 ET is after close
    entry = {**EDGAR_OK, "updated": "2026-07-10T16:45:00-04:00"}
    assert "friday_pm" in normalize_edgar(entry).channels


def test_edgar_thursday_no_friday_flag():
    entry = {**EDGAR_OK, "updated": "2026-07-09T16:45:00-04:00"}
    assert "friday_pm" not in normalize_edgar(entry).channels


def test_edgar_no_id_quarantines():
    bad = {**EDGAR_OK, "id": "", "link": ""}
    with pytest.raises(NormalizeError) as e:
        normalize_edgar(bad)
    assert e.value.reason_code == "MISSING_REQUIRED_FIELD"


# ---- rss --------------------------------------------------------------------------

RSS_OK = {
    "title": "Acme Corp said to weigh sale to larger rival",
    "id": "https://marketpulse.example/acme-777",
    "link": "https://marketpulse.example/acme-777",
    "published": "2026-07-07T14:29:51Z",
    "summary": "Unconfirmed report of strategic alternatives.",
}


def test_rss_ok():
    item = normalize_rss(RSS_OK, feed_name="marketpulse")
    assert item.item_id.startswith("rss:marketpulse:")
    assert item.source == "rss:marketpulse"
    assert item.source_tier == 3


def test_rss_stable_item_id():
    a = normalize_rss(RSS_OK, feed_name="marketpulse")
    b = normalize_rss(RSS_OK, feed_name="marketpulse")
    assert a.item_id == b.item_id                # same guid -> same id across polls


def test_rss_missing_ts_quarantines():
    bad = {k: v for k, v in RSS_OK.items() if k != "published"}
    with pytest.raises(NormalizeError) as e:
        normalize_rss(bad, feed_name="marketpulse")
    assert e.value.reason_code == "MISSING_REQUIRED_FIELD"


# ---- contract round-trip -------------------------------------------------------------

def test_payload_is_contract_shaped():
    item = normalize_alpaca(ALPACA_OK)
    p = item.payload()
    for key in ("item_id", "revision", "source", "source_tier", "headline",
                "content_hash", "symbols", "published_ts", "received_ts"):
        assert key in p, f"missing contract field {key}"
    assert p["published_ts"].endswith("Z")
    assert "raw" not in p                        # raw stays in the DB, not on the queue

```

## `tests/unit/test_risk_exec.py`

```python
"""Phase 4 unit tests: the A3 sizing chain (every veto, every clip, the
viability rule), discretion band validation, limit pricing, FakeBroker
semantics. No DB."""
import asyncio
import json

import pytest

from a3_risk.sizing import (SizingInputs, limit_price_from_snapshot,
                            open_risk_dollars, size_entry)
from a3_risk.service import validate_adjustments
from common.broker import BrokerReject, FakeBroker

CAPITAL = {"risk_per_trade_pct": 0.005, "max_position_notional_pct": 0.15,
           "max_portfolio_heat_pct": 0.03,
           "heat_split": {"SHORT": 0.02, "LONG": 0.01},
           "max_sector_heat_pct": 0.015, "min_viable_risk_fraction": 0.5}
LIMITS = {"max_trades_per_day_default": 5, "adv_participation_max": 0.01,
          "spread_max_bps": 40, "entry_blackout_final_min": 15}
PROFILE = {"initial_stop": {"method": "atr", "k": 2.0},
           "catastrophe": {"method": "atr", "k": 3.5},
           "breakeven_at_R": 1.0,
           "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
           "time_stop": {"window": "thesis", "min_progress_R": 0.5},
           "realization": {"target_fraction": 0.7, "action": "scale_out_50"},
           "earnings_blackout_exit": True, "overnight_hold": "eod_rule_v1"}
BANDS = {"k": [1.5, 2.5], "realization_fraction": [0.5, 0.9],
         "time_window_sessions": [1, 3]}


def inputs(**over):
    base = dict(effective_capital=50_000.0, settled_cash=50_000.0,
                ref_price=100.0, bid=99.98, ask=100.02, spread_bps=4.0,
                atr_14=1.5, adv_20d=5_000_000.0,
                open_heat={"SHORT": 0.0, "LONG": 0.0},
                deployed_notional=0.0, trades_today=0,
                minutes_to_close=120)
    base.update(over)
    return SizingInputs(**base)


def run(inp, k=2.0, horizon="SHORT"):
    return size_entry(inp, CAPITAL, LIMITS, PROFILE, horizon, k)


# ---- the happy path ---------------------------------------------------------

def test_clean_size():
    # ATR 2.0 -> stop distance 4.0 -> 62 shares, no clip binds
    r = run(inputs(atr_14=2.0))
    assert r.verdict == "SIZE" and r.qty == 62
    assert r.risk_budget == 250.0
    assert r.actual_risk == pytest.approx(248.0)
    assert r.initial_stop == pytest.approx(r.limit_price - 4.0, abs=0.01)
    assert r.catastrophe_stop == pytest.approx(r.limit_price - 7.0, abs=0.01)
    assert r.numbers["binding_clip"] is None
    assert "EARNINGS_UNKNOWN" in r.flags and "SECTOR_UNKNOWN" in r.flags


def test_default_fixture_notional_trim_is_viable():
    # At ATR=1.5%-of-price the risk-derived size is ~16.7% notional; the 15%
    # cap trims it slightly and the trade stays viable (design observation).
    r = run(inputs())
    assert r.verdict == "SIZE" and r.numbers["binding_clip"] == "notional"
    assert r.actual_risk >= 0.85 * r.risk_budget


def test_limit_price_buffer_capped_at_10bps():
    # spread 4bps -> half-spread 2bps buffer
    assert limit_price_from_snapshot(100.0, 4.0) == pytest.approx(100.02)
    # spread 40bps -> half is 20bps but cap at 10bps
    assert limit_price_from_snapshot(100.0, 40.0) == pytest.approx(100.10)


# ---- hard-gate vetoes -------------------------------------------------------

@pytest.mark.parametrize("field,value,reason", [
    ("kill_switch", True, "KILL_SWITCH"),
    ("breaker", True, "BREAKER"),
    ("block_entries", True, "BLOCK_ENTRIES"),
    ("ticker_halted", True, "HALTED"),
    ("trades_today", 5, "MAX_TRADES"),
    ("minutes_to_close", 10, "ENTRY_BLACKOUT"),
    ("spread_bps", 55.0, "WIDE_SPREAD"),
    ("atr_14", None, "NO_ATR"),
])
def test_hard_gate_vetoes(field, value, reason):
    r = run(inputs(**{field: value}))
    assert (r.verdict, r.veto_reason) == ("VETO", reason)


def test_earnings_blackout_when_known():
    r = run(inputs(earnings_next_sessions=1))
    assert r.veto_reason == "EARNINGS_BLACKOUT"


def test_earnings_unknown_allows_with_flag():
    r = run(inputs(earnings_next_sessions=None))
    assert r.verdict == "SIZE" and "EARNINGS_UNKNOWN" in r.flags


def test_max_trades_respects_operational_control():
    r = size_entry(inputs(trades_today=7, max_trades_per_day=10),
                   CAPITAL, LIMITS, PROFILE, "SHORT", 2.0)
    assert r.verdict == "SIZE"          # dashboard raised the throttle


# ---- clips ------------------------------------------------------------------

def test_notional_clip_then_viability_veto():
    # tiny ATR -> huge raw qty -> 15% notional cap binds -> clipped risk is
    # trivial vs intended -> the viability rule correctly kills the trade
    r = run(inputs(atr_14=0.05))
    assert (r.verdict, r.veto_reason) == ("VETO", "SIZE_CLIPPED")
    assert r.numbers["binding_clip"] == "notional"
    assert r.numbers["qty"] == 74                # 7500 / 100.02ish
    assert r.numbers["actual_risk"] < 0.5 * r.numbers["risk_budget"]


def test_adv_clip_binds():
    r = run(inputs(atr_14=0.05, adv_20d=5_000))
    assert r.numbers["binding_clip"] == "adv"
    assert r.numbers["qty"] == 50                # 1% of 5000 ADV
    assert r.veto_reason == "SIZE_CLIPPED"       # and viability kills it


def test_settled_cash_clip():
    r = run(inputs(atr_14=0.05, settled_cash=2_000.0))
    assert r.numbers["binding_clip"] == "settled_cash"


def test_lane_heat_exhaustion_clips_to_zero():
    # SHORT lane cap = 2% * 50k = 1000; already used 900 -> headroom 100/3.0=33
    r = run(inputs(open_heat={"SHORT": 900.0, "LONG": 0.0}))
    assert r.verdict == "VETO" and r.veto_reason == "SIZE_CLIPPED"
    assert r.numbers["binding_clip"] == "lane_heat"


def test_total_heat_counts_both_lanes():
    # total cap 1500; long lane holds 1290 -> total headroom 210 -> 70 shares,
    # tighter than the 74.97-share notional cap; still viable (210 >= 125)
    r = run(inputs(open_heat={"SHORT": 0.0, "LONG": 1290.0}))
    assert r.verdict == "SIZE"
    assert r.numbers["binding_clip"] == "total_heat"
    assert r.qty == 70


def test_capital_headroom_preflight_clip():
    r = run(inputs(deployed_notional=49_000.0, atr_14=0.05))
    assert r.numbers["binding_clip"] == "capital_headroom"


def test_size_clipped_viability():
    # heat leaves 100/3.0 = 33 shares = $99 actual vs $250 intended -> <50%
    r = run(inputs(open_heat={"SHORT": 900.0, "LONG": 0.0}))
    assert r.veto_reason == "SIZE_CLIPPED"
    assert r.numbers["actual_risk"] < 0.5 * r.numbers["risk_budget"]


def test_open_risk_house_money_is_zero():
    assert open_risk_dollars(100, 50.0, 48.0) == 200.0
    assert open_risk_dollars(100, 50.0, 51.0) == 0.0   # stop above entry


# ---- discretion bands -------------------------------------------------------

def adj_json(**over):
    base = {"k": 2.0, "realization_fraction": 0.7,
            "time_window_sessions": 2, "reason": "clean confirmation"}
    base.update(over)
    return json.dumps(base)


def test_adjustments_within_bands():
    a = validate_adjustments(adj_json(k=1.5), BANDS)
    assert a.k == 1.5


@pytest.mark.parametrize("over", [{"k": 3.0}, {"k": 1.0},
                                  {"realization_fraction": 0.95},
                                  {"time_window_sessions": 5}])
def test_adjustments_outside_bands_rejected(over):
    with pytest.raises(ValueError):
        validate_adjustments(adj_json(**over), BANDS)


# ---- FakeBroker semantics ----------------------------------------------------

def test_fakebroker_fill_and_position():
    async def go():
        b = FakeBroker(settled_cash=10_000)
        o = await b.submit_limit("ACME", "BUY", 50, 100.0, "coid-1")
        assert o.status == "filled" and o.filled_avg_price == 100.0
        pos = await b.get_positions()
        assert pos[0].qty == 50
        acct = await b.get_account()
        assert acct.settled_cash == 5_000
    asyncio.run(go())


def test_fakebroker_idempotent_client_order_id():
    async def go():
        b = FakeBroker()
        o1 = await b.submit_limit("ACME", "BUY", 50, 100.0, "coid-x")
        o2 = await b.submit_limit("ACME", "BUY", 50, 100.0, "coid-x")
        assert o1.broker_order_id == o2.broker_order_id
        assert len(b.submissions) == 1
    asyncio.run(go())


def test_fakebroker_stop_rests_then_manual_fill():
    async def go():
        b = FakeBroker()
        await b.submit_limit("ACME", "BUY", 50, 100.0, "e1")
        s = await b.submit_stop("ACME", "SELL", 50, 95.0, "cat-e1")
        assert s.status == "accepted"
        b.fill_order(s.broker_order_id, price=94.8)
        assert (await b.get_order(s.broker_order_id)).status == "filled"
        assert await b.get_positions() == []       # flat after stop fill
    asyncio.run(go())


def test_fakebroker_reject_and_partial():
    async def go():
        b = FakeBroker()
        b.set_behavior("bad", "reject")
        with pytest.raises(BrokerReject):
            await b.submit_limit("ACME", "BUY", 10, 100.0, "bad")
        b.set_behavior("p1", "partial:20")
        o = await b.submit_limit("ACME", "BUY", 50, 100.0, "p1")
        assert o.status == "partially_filled" and o.filled_qty == 20
    asyncio.run(go())

```

## `tests/unit/test_triage_router.py`

```python
"""Phase 2 unit tests: schema validation, the four routing rules as a matrix,
priority_score, NYSE calendar, prompt assembly. No database required."""
import json
from datetime import datetime, timezone

import pytest

from a1_triage.prompt import build_messages
from a1_triage.schema import (TriageOutput, TriageValidationError,
                              triage_json_schema, validate_triage)
from router.facts import RoutingFacts, market_open_now, priority_score
from router.rules import route

ROUTER_CFG = {
    "tier_weight": {1: 6, 2: 4, 3: 1},
    "urgency_weight": {"high": 6, "medium": 3, "low": 0},
    "corroboration_bonus_per_outlet": 1,
    "corroboration_bonus_cap": 3,
    "overnight_base": 50,
}


# ---- schema ---------------------------------------------------------------------

def test_valid_triage_parses():
    t = validate_triage(json.dumps({
        "material": True, "tickers": ["acme", "ACME", "TOOLONGSYM"],
        "direction_hint": "up", "urgency": "high",
        "novelty_score": 0.9, "reason": "M&A approach"}))
    assert t.tickers == ["ACME"]           # uppercased, deduped, implausible dropped


def test_not_json_raises():
    with pytest.raises(TriageValidationError) as e:
        validate_triage("I think this is material because...")
    assert "not valid JSON" in e.value.detail


def test_schema_violation_raises():
    with pytest.raises(TriageValidationError) as e:
        validate_triage(json.dumps({"material": "yes", "reason": "x"}))
    assert "material" in e.value.detail


def test_extra_field_rejected():
    with pytest.raises(TriageValidationError):
        validate_triage(json.dumps({
            "material": True, "reason": "x", "magnitude_est": 0.05}))  # A2's field, not A1's


def test_novelty_bounds():
    with pytest.raises(TriageValidationError):
        validate_triage(json.dumps({"material": False, "reason": "x", "novelty_score": 1.5}))


def test_json_schema_generates():
    s = triage_json_schema()
    assert s["properties"]["material"]["type"] == "boolean"
    assert "reason" in s["required"]


# ---- routing rules matrix -----------------------------------------------------------

def _t(material=True, tickers=("ACME",), urgency="medium", novelty=0.8):
    return TriageOutput(material=material, tickers=list(tickers),
                        direction_hint="up", urgency=urgency,
                        novelty_score=novelty, reason="test")


def _f(market_open=True, position_ids=(), score=10):
    return RoutingFacts(market_open=market_open, position_ids=list(position_ids),
                        thesis_matches=[], priority_score=score)


def test_rule2_discard_no_routes():
    d = route(_t(material=False), _f())
    assert d.action == "DISCARD" and d.routes == ()


def test_rule2_discard_with_held_position_still_guards():
    """Immaterial-but-held: guard fan-out survives the discard (correction on
    a held name must reach A12)."""
    d = route(_t(material=False), _f(position_ids=[41]))
    assert d.action == "DISCARD"
    assert [r.queue for r in d.routes] == ["signal.guard"]
    assert d.routes[0].priority == 0


def test_rule3_no_ticker_goes_thesis():
    d = route(_t(tickers=()), _f(market_open=True))
    assert [r.queue for r in d.routes] == ["signal.thesis"]   # never intraday


def test_rule4_market_open_analyst():
    d = route(_t(), _f(market_open=True))
    assert [r.queue for r in d.routes] == ["signal.analyst"]


def test_rule4_market_closed_overnight_priority():
    d = route(_t(), _f(market_open=False, score=12), overnight_base=50)
    assert [r.queue for r in d.routes] == ["signal.overnight"]
    assert d.routes[0].priority == 38             # 50 - 12; higher score = claimed earlier


def test_rule1_guard_in_addition_to_normal():
    d = route(_t(), _f(market_open=True, position_ids=[41, 42]))
    assert [r.queue for r in d.routes] == ["signal.guard", "signal.analyst"]


def test_priority_floor_zero():
    d = route(_t(), _f(market_open=False, score=999))
    assert d.routes[0].priority == 0


# ---- priority score ------------------------------------------------------------------

def test_priority_score_composition():
    # tier1(6) + high(6) + round(0.9*4)=4 + outlets 3 -> bonus min(2,3)=2 => 18
    assert priority_score(1, "high", 0.9, 3, ROUTER_CFG) == 18


def test_priority_score_floor():
    assert priority_score(3, "low", 0.0, 1, ROUTER_CFG) == 1


def test_corroboration_bonus_capped():
    assert priority_score(3, "low", 0.0, 99, ROUTER_CFG) == 1 + 3


# ---- market calendar ------------------------------------------------------------------

def test_nyse_holiday_closed():
    # 2026-07-03 (Friday) is the Independence Day observed holiday
    assert not market_open_now(datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc))


def test_nyse_regular_tuesday_open():
    # Tuesday 2026-07-07 15:00 UTC = 11:00 ET
    assert market_open_now(datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc))


def test_nyse_after_close():
    # Tuesday 2026-07-07 21:00 UTC = 17:00 ET
    assert not market_open_now(datetime(2026, 7, 7, 21, 0, tzinfo=timezone.utc))


# ---- prompt ---------------------------------------------------------------------------

def test_prompt_includes_item_and_fewshot():
    msgs = build_messages({"headline": "H", "summary": "S", "source": "edgar",
                           "source_tier": 1, "symbols": [], "channels": ["8-K"]},
                          {"is_new_story": True, "independent_outlets": 1})
    assert msgs[0]["role"] == "system"
    assert len(msgs) == 1 + 3 * 2 + 1              # system + 3 shots + user
    assert '"headline": "H"' in msgs[-1]["content"]


def test_prompt_retry_appends_error():
    msgs = build_messages({"headline": "H"}, {}, retry_error="schema violations: material: field required")
    assert "previous response was invalid" in msgs[-1]["content"]

```
