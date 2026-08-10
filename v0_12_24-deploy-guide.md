# Deploy Guide — v0.12.24 (macro/economy lane)

**What this release does:** gives the system its first view of the economy.
A new nightly job pulls 19 economic series (rates, the yield curve,
inflation, jobs, credit stress, the dollar, oil) with 5 years of history
from the Federal Reserve's public FRED database — no account or key needed
— and compiles them into a compact economic picture that the thesis bot
reads on every run. Three official news feeds (Federal Reserve, Bureau of
Labor Statistics, Energy Information Administration) also join your news
pipeline, so macro *stories* flow to the thesis lane the same way company
news already does.

**Risk: low-medium.** One additive database migration, one service restart
(the news ingester — do it outside market hours), one new timer. The
thesis bot itself needs no restart. Everything degrades gracefully: if
FRED or a feed is unreachable, the affected piece marks itself degraded
and the rest of the system doesn't notice.

**Time: ~25 minutes.** **When: this evening after 3:00 PM your time** (the
ingester restart must not happen during market hours).

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_24-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `v0_12_24-pack` containing `src`, `config`, `ops`, `schema`, `tests`
   and two loose `.md` files.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in **src**, **config**, **ops**, **schema**, **tests** and the two
   `.md` files.
3. **Seven files are REPLACED:** `config/sources.yaml`, `config/a5.yaml`,
   `src/c1_ingestion/normalize.py`, `src/c1_ingestion/sources/rss.py`,
   `src/a5_thematic/prompt.py`, `src/a5_thematic/service.py`,
   `tests/unit/test_a5_bootstrap.py`. Nine are NEW.
4. Commit message: `v0.12.24: macro/economy lane`
5. Commit, open the commit, confirm **16 changed files**. Anything
   missing → stop, tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.23"` →
   `version = "0.12.24"` → commit.
2. **Releases → Draft a new release** → tag `v0.12.24` → title
   `v0.12.24 — macro/economy lane` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.24
```

## Part 5 — Database migration (one additive table)

Test database first, then the real one:

```bash
sudo -u postgres psql -d trading_test -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/011-macro-series.sql
sudo -u postgres psql -d trading -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/011-macro-series.sql
```

Each should end with `COMMIT` and no `ERROR` lines.

## Part 6 — Run the test suite

```bash
sudo rm -rf /tmp/qdrant-*
cd /opt/pipeline
sudo -u trader bash -c 'export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test MARKETDATA=fake BROKER=fake \
  PIPELINE_DSN=postgresql://trader:PASSWORD@127.0.0.1:5432/trading_test && .venv/bin/python -m pytest tests/ -q 2>&1 | tail -5'
```

(The `rm -rf /tmp/qdrant-*` first — we learned that lesson last night.)
Expect: **the same 8 date-drift failures as v0.12.23, and 23 more tests
passed** (627 passed if last night's count was 604). Any NEW failure →
stop, copy the last 30 lines to Claude.

## Part 7 — Install the timer, restart the ingester (AFTER market close)

```bash
sudo cp /opt/pipeline/ops/systemd/macro-fetch.service /opt/pipeline/ops/systemd/macro-fetch.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now macro-fetch.timer
sudo systemctl restart c1-ingestion
systemctl list-timers 'macro-*' --no-pager
```

The NEXT column should show **tonight 19:45 your time** (20:45 ET).

## Part 8 — Prove it now (don't wait for the timer)

**8a. Backfill the economic data** (one command, takes ~1 minute — 19
series, 5 years each):

```bash
sudo systemctl start macro-fetch.service
sudo journalctl -u macro-fetch -n 5 --no-pager
```

The last line should say `macro refresh done` with `series_ok=19
series_failed=0` and `rows=` in the tens of thousands. Then look at it:

```bash
sudo -u postgres psql -d trading -c "SELECT series_id, count(*) AS obs, max(obs_date) AS newest FROM news.macro_series GROUP BY series_id ORDER BY series_id;"
```

19 rows, each with `newest` within the last few weeks. (Tonight's 19:45
timer firing will then only top up the tail — that's the incremental
design, not a failure.)

**8b. Check the three news feeds took** (a few minutes after the restart):

```bash
sudo -u postgres psql -d trading -c "SELECT component, status, detail FROM journal.health WHERE component LIKE 'ingestion:rss%' ORDER BY component;"
```

You want `ingestion:rss:fed-monetary`, `ingestion:rss:bls-releases` and
`ingestion:rss:eia-today` present and `OK`. **If one of them is DEGRADED**,
that feed's URL has moved — copy the detail text to Claude; the other
feeds and everything else are unaffected in the meantime.

And see the first macro items arrive (may be sparse — these publishers
post a handful of items a day):

```bash
sudo -u postgres psql -d trading -c "SELECT source, source_tier, headline FROM news.news_items WHERE source IN ('rss:fed-monetary','rss:bls-releases','rss:eia-today') ORDER BY received_ts DESC LIMIT 10;"
```

**8c. Tonight at 20:30 CT**, A5 runs as usual. In tomorrow's check:

```bash
sudo -u postgres psql -d trading -c "SELECT payload->>'macro_context' AS macro, payload->>'new_theses' AS new, payload->>'evidence_attached' AS ev FROM journal.decisions WHERE stage='THEMATIC' AND action='DIGEST' ORDER BY ts DESC LIMIT 1;"
```

`macro = true` means the thesis bot saw the economic picture. From now on
its evidence notes and new-thesis drivers can cite rates, credit and
inflation — watch the digest emails for exactly that.

## What this sets up next

- The A8 morning briefing could carry a one-line macro dashboard (not in
  this release — kept focused).
- The C8 regime tagger could eventually use `news.macro_series` instead
  of/alongside market-derived features.
- Optional at any time: a free FRED API key in
  `/etc/pipeline/pipeline.env` as `FRED_KEY=...` switches the fetcher to
  the official API automatically. Not required.

## Rollback

```bash
sudo systemctl disable --now macro-fetch.timer
sudo -u trader git -C /opt/pipeline checkout v0.12.23
sudo systemctl restart c1-ingestion
```

Leave migration 011 in place (additive, harmless).
