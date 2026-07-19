# Deploy Guide — v0.11.0 (Phase 9: A8 Morning Briefing)

**What this release does:** replaces the bare pre-market sheet email with
ONE consolidated morning briefing at 6:35 AM your time: the ranked
overnight candidates, your open positions with their earnings countdowns
and last night's A6 recommendations, the standing thesis store, today's
earnings landscape, and system health — topped with a short model-written
summary of what actually needs your attention. This is the last email in
the system's design; after this, your inbox rhythm is: morning briefing →
EOD report → (only if action is recommended) evening review alert →
thesis digest.

**When to do this:** tonight after v0.10.0, or any evening. ~10 minutes.
No database changes, no new keys, no new permissions. **Deploy v0.9.0 and
v0.10.0 FIRST** — this builds on both.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_11_0-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `v0_11_0-pack` containing `src`, `config`, `ops`, `tests` and the two
   loose `.md` files.

## Part 2 — Upload to GitHub (browser, same as before)

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in **src**, **config**, **ops**, **tests** and the two `.md`
   files.
3. **Three files are REPLACED this time** (GitHub handles it
   automatically):
   - `src/a4_premarket/service.py` — its email becomes optional
   - `config/a4.yaml` — turns that email off (A8 takes over)
   - `tests/integration/test_premarket_flow.py` — updated for the flag
4. Commit message: `v0.11.0: Phase 9 — A8 morning briefing`
5. **Commit changes**, then open the commit and confirm **15 changed
   files** (12 new + 3 changed), including
   `src/a8_briefing/service.py`. Anything missing → stop, tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil → `version = "0.10.0"` →
   `version = "0.11.0"` → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.11.0` → title
   `v0.11.0 — A8 morning briefing` → **Publish**.

## Part 4 — Pull onto the Spark and test

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.0
cd /opt/pipeline
sudo -u trader bash -c 'export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test MARKETDATA=fake BROKER=fake \
  PIPELINE_DSN=postgresql://trader:PASSWORD@127.0.0.1:5432/trading_test && .venv/bin/python -m pytest tests/ -q'
```

Expect **364 passed, 0 failed** (7 more than v0.10.0). Any failure →
stop, copy the last 30 lines to Claude.

## Part 5 — Install the timer

```bash
sudo cp /opt/pipeline/ops/systemd/a8-briefing.service /opt/pipeline/ops/systemd/a8-briefing.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now a8-briefing.timer
systemctl list-timers a8-briefing.timer --no-pager
```

NEXT should show the next weekday at **06:35 your time** (07:35 ET).

## Part 6 — What changes tomorrow morning

- **06:35 CT:** ONE email, subject `Morning briefing 2026-07-20 — ...`.
  The old separate "Pre-market ..." email no longer arrives — its content
  is the first section of the briefing.
- The briefing arrives even if parts of the system had a bad night — a
  missing section says so explicitly (e.g. "PRE-MARKET SHEET: not
  available yet") instead of silently disappearing. If you see one of
  those lines, tell Claude what it said.
- If you ever want the old separate sheet email back:
  `sudo nano /opt/pipeline/config/a4.yaml` → change `email: false` to
  `email: true` under `report:`. (Both emails will then arrive.)

## Rollback (if anything misbehaves)

```bash
sudo systemctl disable --now a8-briefing.timer
sudo -u trader git -C /opt/pipeline checkout v0.10.0
```

v0.10.0's A4 always sends its own sheet email, so rolling back
automatically restores the old morning email. Nothing else to undo.
