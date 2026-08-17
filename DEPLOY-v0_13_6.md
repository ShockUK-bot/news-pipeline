# Deploy Guide — v0.13.6 (fix the too-tight staleness limits)

**What this does:** stops feeds being marked yellow for being quiet over a
weekend. The limits I set in v0.13.3 were shorter than a normal weekend, so
they were always going to false-alarm. This replaces them with numbers
measured from the feeds themselves.

**Config and test files only. No pipeline code changes.**

**Time:** about 10 minutes. Any time.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_13_6-pack.zip` from the chat.
2. Right-click → **Extract All** → into a **NEW empty folder**.
3. You'll get `v0_13_6-pack` with a **config** folder, a **tests** folder,
   and two loose `.md` files.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **config** folder, the **tests** folder, and both `.md` files.
3. **Two files are REPLACED:** `config/sources.yaml` and
   `tests/unit/test_rss_hardening.py`.
4. Commit message: `v0.13.6: re-derive staleness limits from measured feed cadence`
5. **Commit changes**, then confirm the commit shows **4 changed files**
   (2 replaced + 2 new).

## Part 3 — Version bump and release

1. `pyproject.toml` → pencil → `version = "0.13.5"` → `version = "0.13.6"`
   → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.13.6` → title
   `v0.13.6 — staleness limits` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.6
```

## Part 5 — Run the tests

```bash
cd /opt/pipeline
```

```bash
export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test
```

```bash
env -u PIPELINE_DSN .venv/bin/python -m pytest tests/unit -q --deselect tests/unit/test_cik_map.py::test_end_to_end_stored_with_symbols
```

**Expected:** `1 failed, 679 passed` — up 2, with the same pre-existing
`test_triage_v047.py` failure and nothing else.

## Part 6 — Restart ingestion

```bash
sudo systemctl restart c1-ingestion
```

```bash
sudo systemctl status c1-ingestion --no-pager
```

## Part 7 — Confirm the yellow row clears

Wait 2–3 minutes, then:

```bash
sudo -u postgres psql -d trading -c "SELECT component, status, detail FROM journal.health WHERE component LIKE 'ingestion:rss%' ORDER BY component;"
```

**What you want:** 16 rows, all `OK`, aggregate reading
`OK / 15 feeds, every 60.0s`. In particular `gnw-management` should no
longer be DEGRADED — it's 82 hours quiet, which is now correctly inside the
allowed window rather than an alarm.

You'll still see `poll retry` and `poll failed (transient)` WARNINGs for
`prnewswire-bustech` in the logs. **That is expected and is not being fixed
this release** — see Part 8.

---

## Part 8 — Two things to check on Monday

Neither is urgent, and neither needs a release unless the answer is bad.

**1. Is `gnw-management` actually dead?**

It has published nothing since Thursday, including all of Friday — a
business day. That's either a quiet Friday or a frozen feed, and it can't be
told apart yet. On Monday afternoon:

```bash
sudo -u postgres psql -d trading -c "SELECT max(published_ts) FROM news.news_items WHERE source='rss:gnw-management';"
```

- **Monday's date** → it was just a quiet Friday. Nothing to do.
- **Still Thursday Aug 13** → the feed is frozen. Tell Claude and it comes
  out, the same way Business Wire did.

**2. Does `prnewswire-bustech` still flap during a busy session?**

Everything so far has been measured on a quiet weekend. During Monday
trading:

```bash
sudo journalctl -u c1-ingestion --since '2 hours ago' --no-pager -l | grep -c "feed=prnewswire-bustech"
sudo -u postgres psql -d trading -c "SELECT count(*), max(published_ts) FROM news.news_items WHERE source='rss:prnewswire-bustech' AND received_ts > now() - interval '2 hours';"
```

If items are arriving steadily, the intermittent 301s are costing latency
and nothing else, which is the current assessment. If the count is near zero
during US market hours while `prnewswire-financial` is busy, that assessment
is wrong and Claude should revisit the fresh-connection retry option.

---

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.5
```

```bash
sudo systemctl restart c1-ingestion
```

The old, too-tight limits come back and `gnw-management` goes yellow again.
