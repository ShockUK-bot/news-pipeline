# Deploy Guide — v0.13.4 (GlobeNewswire User-Agent fix + test fix)

**What this does:** fixes the GlobeNewswire timeouts you saw in the
v0.13.3 probe. The cause was our own User-Agent — GlobeNewswire's CDN
deliberately stops answering anything pretending to be Chrome. This gives
those eight feeds an honest one instead. It also fixes the test that broke
during the v0.13.3 deploy.

**Config and test files only. No pipeline code changes.**

**Important — this is the release you restart on.** You checked out
v0.13.3 but never restarted, so your service is still running v0.13.2 code.
Deploy this, then restart once, and you get v0.13.3 and v0.13.4 together.

**When:** any time, but a weekend or after the close is still preferable —
the Nasdaq halt feed arrives with a backlog, and out of hours those items
are all from a previous day and get stamped accordingly.

**Time:** about 15 minutes.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_13_4-pack.zip` from the chat.
2. Right-click → **Extract All** → into a **NEW empty folder**.
3. You'll get `v0_13_4-pack` containing a **config** folder, a **tests**
   folder, and two loose `.md` files.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **config** folder, the **tests** folder, and both `.md` files.
3. **Three files are REPLACED:**
   - `config/sources.yaml`
   - `tests/unit/test_macro.py`
   - `tests/unit/test_rss_hardening.py`
4. Commit message: `v0.13.4: honest UA for globenewswire (Akamai tarpits browser UA), fix test_macro`
5. **Commit changes**, then open the commit and confirm **5 changed files**
   (3 replaced + 2 new). A different number → stop, tell Claude.

## Part 3 — Version bump and release

1. `pyproject.toml` → pencil → `version = "0.13.3"` → `version = "0.13.4"`
   → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.13.4` → title
   `v0.13.4 — GlobeNewswire UA fix` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.4
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

**Expected:** `1 failed, 672 passed`, and the single failure should be
`test_triage_v047.py::test_confidence_required` — the pre-existing one,
explained in Part 9.

If `test_macro.py` or `test_rss_hardening.py` appear in the failures, stop
and paste the output to Claude.

(Part 8 explains why one test is deselected.)

## Part 6 — Re-run the feed probe — this is the one that matters

This version reads `config/sources.yaml` and sends **each feed its own
configured User-Agent**, so it tests exactly what the poller will do rather
than approximating it. Paste the whole block:

```bash
cd /opt/pipeline && env -u PIPELINE_DSN .venv/bin/python - <<'PY'
import httpx, feedparser, yaml
cfg = yaml.safe_load(open("config/sources.yaml"))["rss"]
BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
ok = bad = 0
for feed in cfg["feeds"]:
    name, url = feed["name"], feed["url"]
    ua = feed.get("user_agent") or BROWSER
    if feed.get("user_agent_env"):
        import os
        ua = os.environ.get(feed["user_agent_env"], "") or ua
    try:
        r = httpx.get(url, headers={"User-Agent": ua}, follow_redirects=True,
                      timeout=httpx.Timeout(connect=10, read=40, write=40, pool=10))
        p = feedparser.parse(r.text)
        title = (p.feed.get("title") or "").strip() or "(EMPTY TITLE)"
        flag = "" if r.status_code == 200 else "  <-- CHECK"
        if r.status_code == 200: ok += 1
        else: bad += 1
        print(f"{name:24} {r.status_code}  items={len(p.entries):3}  {title[:40]}{flag}")
    except Exception as e:
        bad += 1
        print(f"{name:24} ERROR {type(e).__name__}: {str(e)[:50]}  <-- CHECK")
print(f"\n{ok} OK, {bad} failing")
PY
```

**What you want to see:** all eight `globenewswire` / `gnw-*` lines now
returning **200 with items**, in a fraction of a second each.

Two lines are expected to look different and are both fine:

- **`nasdaq-halts` with `items=0`** — nothing is halted right now. Good.
- **`bls-latest`** may fail if `BLS_USER_AGENT` isn't in this shell's
  environment. It's read from `/etc/pipeline/pipeline.env` by the service
  itself, so the service will be fine even if this probe isn't.

If any `gnw-*` line still times out, **stop and tell Claude** — don't
restart. That would mean the UA fix didn't hold and I want to see it before
you run on it.

## Part 7 — Restart ingestion (the first restart since v0.13.2)

```bash
sudo systemctl restart c1-ingestion
```

```bash
sudo systemctl status c1-ingestion --no-pager
```

Look for `Active: active (running)`. If it says `failed` or keeps
restarting, stop and send Claude:

```bash
sudo journalctl -u c1-ingestion -n 30 --no-pager
```

Then wait 2–3 minutes and check:

```bash
sudo journalctl -u c1-ingestion --since '3 min ago' --no-pager -l | grep -i "poll stored\|poll failed\|feed stale\|poll retry"
```

- `poll stored` lines with the new feed names = news is flowing.
- `poll retry` and `poll failed (transient)` at WARNING = **normal**, that's
  the new tolerance doing its job.
- `poll failed` at **ERROR** = that feed has missed three polls in a row.
  Worth telling Claude.

On the dashboard you should see a row per feed, and the eight
GlobeNewswire ones should be green rather than yellow. The old
`ingestion:rss:prnewswire-news` row will linger, stale, because nothing
writes to it any more — cosmetic. To clear it:

```bash
sudo -u postgres psql -d pipeline -c "DELETE FROM journal.health WHERE component='ingestion:rss:prnewswire-news';"
```

Finally, confirm the halt feed produced sensible rows (only meaningful if
there have been halts recently):

```bash
sudo -u postgres psql -d pipeline -c "SELECT published_ts, symbols, headline FROM news.news_items WHERE source='rss:nasdaq-halts' ORDER BY received_ts DESC LIMIT 10;"
```

You want headlines like `Trading halt: TALK — Talkspace, Inc. Common Stock
[T12]` with the ticker in `symbols` (e.g. `{TALK}`), not an empty column.
Empty on every row → tell Claude, it means Nasdaq changed their field names.

---

## Part 8 — Why one test is deselected

`test_cik_map.py::test_end_to_end_stored_with_symbols` genuinely writes to
the database — it lives in `tests/unit/` but is really an integration test.
When I told you to run with `env -u PIPELINE_DSN`, I removed the setting it
needs.

Your suite guard (added after the 14 July production-database incident)
refuses to run at all if `PIPELINE_DSN` points anywhere not ending in
`_test`, because integration fixtures TRUNCATE tables. **That guard is
correct and you should never route around it by pointing tests at
`trading`.**

Two options, neither urgent:

- **A (what Part 5 does):** skip that one test. Fine for deploys; you lose
  one end-to-end check of EDGAR ticker stamping.
- **B:** create a `trading_test` database once, and the whole suite runs.
  Say the word and I'll write it up step by step — about 10 minutes, once.

## Part 9 — The pre-existing triage failure

`test_triage_v047.py::test_confidence_required` is an A1 triage schema
test. Nothing in v0.13.3 or v0.13.4 touches triage. The validator now
reports `tickers: Field required; direction_hint: Field required; ...`, so
`confidence` has been pushed out of the shortened message the test
searches — which points at an earlier release adding required triage
fields.

**Check rather than take my word for it.** One minute:

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.2
```

```bash
cd /opt/pipeline && env -u PIPELINE_DSN .venv/bin/python -m pytest tests/unit/test_triage_v047.py -q
```

- **Fails on v0.13.2 too** → pre-existing, unrelated. Send me the full
  failure text and I'll fix it in its own release.
- **Passes on v0.13.2** → I'm wrong, something in v0.13.3 caused it. Tell
  me immediately.

Then put yourself back:

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.4
```

If you do this check **after** Part 7, restart ingestion again afterwards
so you're not left running v0.13.2 code:

```bash
sudo systemctl restart c1-ingestion
```

---

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.3
```

```bash
sudo systemctl restart c1-ingestion
```

The eight GlobeNewswire feeds go back to timing out; everything else stays.
No database changes, no new settings, no new permissions.
