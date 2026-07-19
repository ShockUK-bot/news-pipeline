# Deploy Guide — v0.9.0 (Phase 8: A5 thesis store + A6 position review)

**What this release does:** two new agents. A5 finally reads the
"no-ticker / long-horizon" news lane that has been piling up since Phase 2
and turns it into a persistent store of standing investment theses — with a
nightly digest email. A6 reviews your open positions twice a day: a 3:45 PM
ET "hold overnight or exit before the close?" check, and a nightly deep
review ("is the original reason for this position still true?") that emails
you only when it recommends action. Neither agent trades — they journal
recommendations; your existing code-side stops and rules stay in charge.

**When to do this: tonight (Sunday).** The first A5 run tonight at 8:30 PM
your time is the Sunday deep pass — it drains the whole thesis backlog and
emails you the first digest. Monday then runs with everything live. Total
time: ~20 minutes. One database migration (additive), no new passwords, no
new permissions.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_9_0-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `v0_9_0-pack` containing `src`, `config`, `ops`, `schema`, `tests` and
   the two loose `.md` files.

## Part 2 — Upload to GitHub (browser, same as before)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload
   files**.
2. Drag in **src**, **config**, **ops**, **schema**, **tests** and the two
   `.md` files from inside the `v0_9_0-pack` folder.
3. **One file is REPLACED this time:** `src/router/facts.py` (the thesis-
   match lookup goes live). GitHub handles the replacement automatically
   when you upload — nothing special to do, just don't skip the `src`
   folder.
4. Commit message: `v0.9.0: Phase 8 — A5 thesis store + A6 position review`
5. **Commit changes**, then open the commit and confirm **28 changed
   files** (27 new + 1 changed), including
   `src/a5_thematic/service.py`, `src/a6_position_review/service.py`, and
   `schema/migrations/004-thesis-store.sql`. Anything missing → stop, tell
   Claude.

## Part 3 — Version bump + release

1. Open `pyproject.toml` → pencil icon → change `version = "0.8.0"` to
   `version = "0.9.0"` → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.9.0` → title
   `v0.9.0 — A5 thesis store + A6 position review` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.9.0
```

## Part 5 — Database migration (new step this release)

One additive migration: the thesis-store tables plus two constraint
widenings. Nothing is deleted or rewritten. Apply to the TEST database
first, then the real one:

```bash
sudo -u postgres psql -d trading_test -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/004-thesis-store.sql
sudo -u postgres psql -d trading -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/004-thesis-store.sql
```

Each command should end with `COMMIT` and no lines containing `ERROR`.
If you see an ERROR line → stop, copy it to Claude. (Running a file twice
prints an error like `relation "theses" already exists` — that just means
it was already applied; it did not break anything.)

## Part 6 — Run the test suite

```bash
cd /opt/pipeline
sudo -u trader bash -c 'export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test MARKETDATA=fake BROKER=fake \
  PIPELINE_DSN=postgresql://trader:PASSWORD@127.0.0.1:5432/trading_test && .venv/bin/python -m pytest tests/ -q'
```

Expect **348 passed, 0 failed** (30 more than v0.8.0). Qdrant `.lock`
errors → `sudo rm -rf /tmp/qdrant-*` and re-run. Any failure → stop, copy
the last 30 lines to Claude.

## Part 7 — Install the three timers

```bash
sudo cp /opt/pipeline/ops/systemd/a5-thematic.service /opt/pipeline/ops/systemd/a5-thematic.timer \
        /opt/pipeline/ops/systemd/a6-eod.service /opt/pipeline/ops/systemd/a6-eod.timer \
        /opt/pipeline/ops/systemd/a6-nightly.service /opt/pipeline/ops/systemd/a6-nightly.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now a5-thematic.timer a6-eod.timer a6-nightly.timer
systemctl list-timers 'a5-*' 'a6-*' --no-pager
```

The NEXT column should show (in your Chicago time):
- `a5-thematic.timer` — **tonight 20:30** (21:30 ET, the Sunday deep pass)
- `a6-eod.timer` — Monday 14:45 (15:45 ET)
- `a6-nightly.timer` — Monday 19:00 (20:00 ET)

No new sudo permissions are needed — A5 and A6 reuse the heavy-model rule
A7 already has, and the analyst wake rule A13 already has.

## Part 8 — Tonight's first run (nothing else to do)

At 8:30 PM your time, A5 will: start the heavy model, bulk-expire thesis-
lane items older than a week, read up to 60 of the freshest ones, write the
first standing theses into the store, stop the heavy model, and email you
**"Thesis digest 2026-07-19 — ..."** within ~10 minutes (via the same C5
mailer as your EOD email).

If you'd rather see it immediately instead of waiting for 8:30, fire it by
hand — the 8:30 firing will then quietly no-op (it's idempotent per day):

```bash
sudo systemctl start a5-thematic.service
journalctl -u a5-thematic.service --no-pager -n 20
```

To watch it work: `journalctl -u a5-thematic.service -f` (Ctrl-C to stop).

## Part 9 — What to watch Monday (first full Phase-1-through-8 day)

- **06:30 CT** — A4 pre-market briefing email (as before).
- **14:45 CT** — A6's EOD check runs. It only does anything if you hold a
  SHORT-lane position at the time; otherwise it journals "no positions"
  and exits. Check: `journalctl -u a6-eod.service --no-pager -n 10`
- **15:35 CT** — A7 EOD report email (as before).
- **19:00 CT** — A6 nightly review; emails ONLY if it recommends
  trimming/exiting something (quiet = good).
- **20:30 CT** — A5 nightly digest email.
- Dashboard decision tape now shows `THEMATIC` and `POSITION_REVIEW` rows.

## Rollback (if anything misbehaves)

```bash
sudo systemctl disable --now a5-thematic.timer a6-eod.timer a6-nightly.timer
sudo -u trader git -C /opt/pipeline checkout v0.8.0
```

Leave migration 004 in place (it's additive and harmless). The thesis lane
just goes back to accumulating, exactly as before Phase 8.
