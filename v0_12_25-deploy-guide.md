# Deploy Guide — v0.12.25 (BLS feed fix)

**What this does:** makes the BLS news feed work. Your probes proved BLS
only admits clients that identify themselves with contact info, and that
the v0.12.24 URL didn't exist. This release points at BLS's real
all-releases feed and lets that one feed introduce itself with your email
— which is stored in the machine's private environment file, never in the
public GitHub repo.

**Risk: minimal.** Three files, no migration, no new services, one new
line in the env file. One ingester restart, outside market hours.
**Time: ~10 minutes.**

---

## Part 1 — Get the pack

Download `v0_12_25-pack.zip` → right-click → **Extract All** into a NEW
empty folder.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in **src**, **config**, **tests** and the two `.md` files.
3. **Three files are REPLACED:** `src/c1_ingestion/sources/rss.py`,
   `config/sources.yaml`, `tests/unit/test_macro.py`.
4. Commit message: `v0.12.25: BLS feed fix — env-sourced user agent + bls_latest URL`
5. Confirm **5 changed files** on the commit.

## Part 3 — Version bump + release

`pyproject.toml` → pencil icon → `version = "0.12.25"` → commit →
**Releases → Draft a new release** → tag `v0.12.25` → **Publish**.

## Part 4 — Add the one new env line (on the Spark)

This is the string your probe proved BLS accepts. It goes in the private
env file (same place as your API keys), NOT in GitHub:

```bash
echo 'BLS_USER_AGENT="news-pipeline/0.12 (contact: ian.gillbanks@gmail.com)"' | sudo tee -a /etc/pipeline/pipeline.env
sudo tail -1 /etc/pipeline/pipeline.env
```

The second command just shows you the line landed (quotes included — they
matter, the value contains spaces).

## Part 5 — Pull + test + restart (after 3:00 PM your time)

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.25
sudo rm -rf /tmp/qdrant-*
cd /opt/pipeline
sudo -u trader bash -c 'export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test MARKETDATA=fake BROKER=fake \
  PIPELINE_DSN=postgresql://trader:PASSWORD@127.0.0.1:5432/trading_test && .venv/bin/python -m pytest tests/unit/test_macro.py tests/unit/test_a5_bootstrap.py -q'
```

Expect **33 passed, 0 failed**. Then:

```bash
sudo systemctl restart c1-ingestion
```

## Part 6 — Confirm (2–3 minutes after the restart)

```bash
sudo -u postgres psql -d trading -c "SELECT component, status, detail FROM journal.health WHERE component LIKE 'ingestion:rss%' ORDER BY component;"
```

You want `ingestion:rss:bls-latest — OK`. (The old `bls-releases` row
stops updating and goes stale; ignore it. If `bls-latest` shows DEGRADED
with a 403, the env line in Part 4 didn't take — check the quotes and
re-run the restart.) Then the items:

```bash
sudo -u postgres psql -d trading -c "SELECT headline, published_ts FROM news.news_items WHERE source='rss:bls-latest' ORDER BY received_ts DESC LIMIT 5;"
```

BLS release headlines = done. **July CPI prints this week** — that
headline will arrive through this feed, get triaged by A1, and reach the
thesis lane the same night A5 sees the CPI number itself land via FRED.
Both halves of the macro lane firing on one event: that's the finish line
for this whole arc.

While you're in the health output: if `ingestion:rss:globenewswire-public`
is still DEGRADED (last night it was a timeout), mention it — if it's OK,
it was transient as expected.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.24
sudo systemctl restart c1-ingestion
```

The BLS_USER_AGENT line is harmless under v0.12.24; no need to remove it.
