# v0.12.24 — Macro/economy lane (2026-08-10)

## Why

Every input the system has ever had is equity news (Alpaca/Benzinga wire,
Polygon, EDGAR, corporate-wire RSS). It has NO view of rates, the yield
curve, inflation, labor, credit spreads, the dollar or energy — the tide
all of its theses swim in. Operator asked for "wider news and economy
impacts" for the thesis layer; this release adds both halves:

1. **Numbers:** a curated set of 19 FRED series stored with 5 years of
   history and refreshed nightly, compiled into a compact `macro_context`
   block that rides in every A5 pack.
2. **Words:** three official macro news feeds (Federal Reserve monetary
   policy, BLS releases, EIA energy) into the EXISTING ingestion pipeline
   — dedup, A1 triage, router rule 3 — so material macro stories flow to
   the thesis lane like any other ticker-less news.

## Half 1 — macro series (`src/c1_ingestion/macro.py`, new)

Modeled directly on the earnings-calendar job:

- **Keyless by default.** Uses FRED's public `fredgraph.csv` endpoint — no
  account, no secret, nothing to configure. If `FRED_KEY` is ever added to
  `/etc/pipeline/pipeline.env` (free key, two minutes to obtain), the
  official API is used automatically instead. Rule 22 respected: the key
  only ever lives in the environment.
- **Series** (`config/macro.yaml`): Fed funds, 2y/10y Treasuries, 2s10s
  curve, CPI headline+core YoY, 5y breakevens, unemployment, payrolls
  (monthly change), jobless claims, retail sales YoY, industrial
  production YoY, consumer sentiment, high-yield credit spread, VIX,
  trade-weighted dollar YoY, WTI crude, 30y mortgage, M2 YoY.
- **Incremental with revision capture:** first fetch backfills
  `store.backfill_years` (5); later fetches re-pull a 14-day tail so
  revised prints (CPI, payrolls) are re-captured via upsert.
- **Failure isolation:** each series fetches independently — one failure
  never kills the run. All series failing → `MACRO_REFRESH_FAILED` +
  'macro' health DEGRADED, never a crash. Success journals ONE
  `SYSTEM/C1 MACRO_REFRESH` row with per-series counts.
- **Timer:** `macro-fetch.timer`, daily 20:45 ET — fresh data on disk 45
  minutes before A5's 21:30 pass. ~19 requests with a 1s pause.
- **Migration 011:** one additive table, `news.macro_series`
  (PK series_id+obs_date). No constraint changes.

## Half 2 — macro news feeds (`config/sources.yaml`, RSS ingester)

Three feeds added: `fed-monetary` (federalreserve.gov press_monetary,
tier 1), `bls-releases` (bls.gov news_release, tier 1), `eia-today`
(eia.gov todayinenergy, tier 2).

To support them, the RSS path gains two small, back-compatible features:

- **Per-feed `tier:` override** (`sources/rss.py`) — official primary
  sources are no longer forced to the block's tier-3 default. The three
  wire feeds keep exactly their old behavior.
- **Per-feed `tags:`** injected into the item's channels
  (`normalize.py: extra_channels`) — every macro item carries `macro` (+
  `fed`/`econ-data`/`energy`) so A1's triage and later analytics can see
  the source class even when the publisher tags nothing.

These items are ticker-less by nature: A1 judges materiality as usual and
router rule 3 sends material ones to `signal.thesis` — never the intraday
path. A dead/renamed feed URL degrades only its own per-feed health row
(v0.11.1 behavior); nothing else notices.

## A5 integration (`a5_thematic/service.py`, `prompt.py`, `config/a5.yaml`)

- `macro_context()` compiles the series into ~2KB of labeled readings with
  1m/3m/1y changes, grouped (rates_curve → inflation → labor → growth →
  credit_risk → dollar_energy → housing_money). Stale series (newest obs
  > 45 days old) are excluded and named. Defensive by contract: missing
  table, empty table, missing config, any error → `None`, and A5 runs
  exactly as pre-v0.12.24.
- Included on EVERY A5 run (config `lane.macro_context: true`) — it helps
  nightly evidence polarity as much as deep-pass authoring.
- Prompt (both modes): use macro to judge whether a driver rows with or
  against the tide, cite it when it shapes the judgment — and do NOT seed
  theses about macro data alone: macro is context; theses need equity
  beneficiaries. Macro shifts MAY be the causal driver behind an equity
  thesis.
- Digest payload gains `macro_context: true|false` so a missing block is
  visible, not silent.

## Files

NEW (9): `schema/migrations/011-macro-series.sql`, `config/macro.yaml`,
`src/c1_ingestion/macro.py`, `ops/systemd/macro-fetch.service`,
`ops/systemd/macro-fetch.timer`, `tests/unit/test_macro.py`,
`tests/integration/test_macro_flow.py`, `patch-notes-v0_12_24.md`,
`v0_12_24-deploy-guide.md`

REPLACED (7): `config/sources.yaml`, `config/a5.yaml`,
`src/c1_ingestion/normalize.py`, `src/c1_ingestion/sources/rss.py`,
`src/a5_thematic/prompt.py`, `src/a5_thematic/service.py`,
`tests/unit/test_a5_bootstrap.py`

**16 changed files** on the upload commit, then the pyproject pencil edit
to `0.12.24`. One additive migration (011). One service restart
(`c1-ingestion` — it reads sources.yaml at startup; restart OUTSIDE market
hours). One new timer (`macro-fetch`). A5 untouched operationally (oneshot;
picks everything up next run). No new secrets required.

## Tests

23 new: 15 unit in `test_macro.py` (both fredgraph header vintages, the
HTML-error-page rejection, junk-row tolerance, FRED API shape, transform
math including the YoY window tolerance, feature deltas, config coherence
pins — unique ids, valid transforms/groups, the core-four regime inputs
present, macro feeds registered at the right tiers — and the
normalize_rss tier/tags injection with a back-compat pin), 2 added to
`test_a5_bootstrap.py` (macro block in the user turn only when supplied;
prompt explains macro in both modes and forbids macro-only theses), and 6
integration in `test_macro_flow.py` on real PG16 (backfill + journal +
health; idempotent incremental re-run; one-bad-series isolation; total-
failure degradation; live context grouping/transform/staleness; None on
empty table).

Sandbox verification: full unit suite green except the same 2 pre-existing
sandbox failures the unmodified tree produces; integration
`test_earnings_flow + test_macro_flow + test_thesis_flow` = **15 passed**
against a real PostgreSQL 16 with migrations 001→011 applied in order
(011 verified to apply cleanly on top of the chain).

## Rollback

```bash
sudo systemctl disable --now macro-fetch.timer
sudo -u trader git -C /opt/pipeline checkout v0.12.23
sudo systemctl restart c1-ingestion
```

Leave migration 011 in place (additive, harmless). A5's `macro_context()`
call disappears with the checkout; the three macro feeds stop being polled
after the restart. Nothing else to undo.
