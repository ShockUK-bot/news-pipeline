# Deploy Guide — v0.11.5 (Stop the sympathy-lane analysis loop)

**What this release does:** fixes the bug where a single news article naming
several tickers (like the Taco Bell / Cava / Chipotle food-safety story today)
gets sent to the analyst over and over — 90+ times an hour — clogging the
analyst queue. After this, one story is analyzed at most once per name.

**When to do this:** as soon as you can. It restarts only `a2-analyst`
(~5 minutes). It touches no database and no other service, so it's safe during
market hours. Do the "Stop it now" step below first if the loop is still
running.

---

## Stop it now (optional but recommended, before deploying)

If the loop is still active, clear the in-flight sympathy messages so the
analyst queue drains immediately. This only drops repeat re-analyses of a story
that's already been vetoed — no positions, no real incoming news are affected:

```bash
psql "$PIPELINE_DSN" -c "DELETE FROM queue.messages WHERE queue_name='signal.synthetic';"
```

Then watch the analyst queue fall back toward normal:

```bash
psql "$PIPELINE_DSN" -c "SELECT queue_name, count(*) FROM queue.messages GROUP BY 1 ORDER BY 2 DESC;"
```

(If `PIPELINE_DSN` isn't set in this terminal, set it first:
`export PIPELINE_DSN=postgresql://trader:<your-password>@127.0.0.1:5432/trading`)

---

## Part 1 — Get the pack onto your PC

1. Download `v0_11_5-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a
   `v0_11_5-pack` folder containing a `src` folder, a `tests` folder, and two
   loose `.md` files.

## Part 2 — Upload to GitHub (browser)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in **all four** items from `v0_11_5-pack`: the **src** folder, the
   **tests** folder, and the two `.md` files. (Drag the folders themselves, not
   the files inside them — that keeps the `src/a2_analyst/service.py` and
   `tests/integration/test_analyst_gate_flow.py` paths intact so GitHub
   replaces the existing files instead of creating new ones.)
3. **Two files are REPLACED this time** (GitHub handles it automatically):
   `src/a2_analyst/service.py` and
   `tests/integration/test_analyst_gate_flow.py`.
4. Commit message: `v0.11.5: cap sympathy fan-out to one hop (stop analysis loop)`
5. **Commit changes**, then open the commit and confirm **4 changed files**
   (2 replaced + 2 new `.md` files). Anything different → stop, tell Claude
   before continuing.

## Part 3 — Version bump + release

1. Open `pyproject.toml` in the repo → pencil (edit) icon → change
   `version = "0.11.4"` to `version = "0.11.5"` → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.11.5` → title
   `v0.11.5 — Stop sympathy-lane analysis loop` → **Publish**.

## Part 4 — Pull onto the Spark, run the test, restart A2

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.5
```

Run the regression test first (this is the guard that keeps the loop from ever
coming back — worth the two minutes):

```bash
sudo -u trader bash -c 'set -a; source /etc/pipeline/pipeline.env; set +a; cd /opt/pipeline && .venv/bin/python -m pytest -q tests/integration/test_analyst_gate_flow.py'
```

Look for a line like `9 passed` with no `failed`. If anything fails, copy the
last 20-30 lines to Claude and do **not** restart the service yet.

Then restart the analyst:

```bash
sudo systemctl restart a2-analyst
sudo systemctl status a2-analyst --no-pager
```

Look for `Active: active (running)`. If it says `failed` or keeps restarting,
stop and copy the last 20 lines of
`sudo journalctl -u a2-analyst -n 20 --no-pager` to Claude.

Note: only `a2-analyst` needs restarting — not A1, not the model servers.

## Part 5 — Confirm it worked

Give it 15–30 minutes of live news, then check that no single ticker is being
re-analyzed dozens of times:

```bash
psql "$PIPELINE_DSN" -c "SELECT ticker, count(*) FROM journal.decisions WHERE stage='ANALYST' AND ts > now() - interval '30 minutes' GROUP BY ticker HAVING count(*) > 3 ORDER BY 2 DESC;"
```

- **No rows, or only small counts (2–3)** → fixed. A multi-ticker story now
  produces the primary analysis plus one per sibling, then stops.
- **A ticker in the dozens again** → tell Claude; paste the trail query from the
  diagnostic runbook for that ticker.

Also confirm the analyst queue stays healthy (should be small during normal
flow, not thousands):

```bash
psql "$PIPELINE_DSN" -c "SELECT queue_name, count(*) FROM queue.messages GROUP BY 1 ORDER BY 2 DESC;"
```

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.4
sudo systemctl restart a2-analyst
```

Nothing else to undo — no database or config changes were made.
