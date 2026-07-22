# Deploy Guide — v0.11.11 (health-row recovery: regime + RSS)

**What this release does:** stops two dashboard lights (`regime` and the RSS
`ingestion:rss` row) from getting stuck red after a brief network hiccup, even
though everything is actually working. From now on those lights turn themselves
back green as soon as the service recovers, and they only go red for a *real*,
sustained problem — not a one-off blip. This is a small, code-only fix: **no
database changes, no new timers, no new permissions, no new keys.**

**When to do this:** any time. ~10 minutes. It's not urgent — you already
cleared the two stuck lights by hand, so this just makes sure they never stick
again. Builds on v0.11.10 (which you're already running).

*(One tiny note: Part 4 restarts your news feed for about 2 seconds. That's
harmless, but if you'd rather have zero interruption, do Part 4 after the market
closes. Everything else can be done any time.)*

---

## Part 1 — Get the pack onto your PC

1. Download `v0_11_11-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a folder
   `v0_11_11-pack` containing a `src` folder, a `tests` folder, and two loose
   `.md` files.

## Part 2 — Upload to GitHub (browser, same as always)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in, from the extracted `v0_11_11-pack` folder: the **src** folder, the
   **tests** folder, and the two **`.md`** files.
3. **Two files are REPLACED** (GitHub handles this automatically):
   - `src/c8_regime/service.py`
   - `src/c1_ingestion/sources/rss.py`

   **Three files are NEW:**
   - `tests/unit/test_health_recovery.py`
   - `patch-notes-v0_11_11.md`
   - `v0_11_11-deploy-guide.md`
4. Commit message: `v0.11.11: health-row recovery for regime + RSS aggregate`
5. **Commit changes**, then open the commit and confirm it shows **5 changed
   files** (2 replaced + 3 new). If it shows anything different — stop and tell
   Claude before going further.

## Part 3 — Version bump + release

1. Open `pyproject.toml` in the repo → pencil (edit) icon → change
   `version = "0.11.10"` to `version = "0.11.11"` → **Commit changes** to `main`.
2. **Releases → Draft a new release** → tag `v0.11.11` → title
   `v0.11.11 — health-row recovery` → **Publish**.

## Part 4 — Pull onto the Spark and restart the two services

Open a terminal on the Spark and run these one at a time:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.11
sudo systemctl restart c8-regime c1-ingestion
```

If the `checkout` line prints an error about "local changes would be
overwritten", **stop** and paste it to Claude — don't force anything.

Then confirm both came back up cleanly:

```bash
sudo systemctl is-active c8-regime c1-ingestion
```

Both lines should print `active`. If either says `failed` or keeps restarting,
stop and paste the last 20 lines of
`sudo journalctl -u c8-regime -n 20 --no-pager` (or `c1-ingestion`) to Claude.

## Part 5 — (Optional but recommended) Run the tests on the Spark

```bash
cd /opt/pipeline
export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test
.venv/bin/python -m pytest tests/unit/test_health_recovery.py -q
```

Expect the last line to say **11 passed**. (If you want the whole suite, run
`.venv/bin/python -m pytest tests/unit -q` — expect the same pass count as
before plus these 11, with only the two long-standing environment-sensitive
failures unchanged. Anything else FAILED → paste it to Claude.)

## Part 6 — Confirm on the dashboard

Give it about a minute, then run this to see the two rows are green **and
staying fresh**:

```bash
psql "$PIPELINE_DSN" -c "SELECT component, status, detail, updated_ts FROM journal.health WHERE component IN ('regime','ingestion:rss') ORDER BY component;"
```

- `regime` should read **OK**, detail like `snapshot 2xx`, with an `updated_ts`
  that moves forward each interval (30 min during market hours, hourly
  off-hours).
- `ingestion:rss` should read **OK**, detail like `3 feeds, every 60s` (or
  `2/3 feeds OK` if one feed happens to be flaky right then), with an
  `updated_ts` that updates every ~60 seconds.

The key difference from before: those timestamps now keep moving. A stuck light
was always a frozen timestamp — that can't happen for these two anymore.

## Rollback (if anything misbehaves)

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.10
sudo systemctl restart c8-regime c1-ingestion
```

Nothing else to undo — no migrations, no timers, no config files were changed.
