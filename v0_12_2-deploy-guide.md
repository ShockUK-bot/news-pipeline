# Deploy Guide — v0.12.2 (promotion rule + MAX TRADES button + Performance tab)

**What this release does:** three things you asked for. (1) **Promotion:**
when a scanner trade's causal news prints after entry — the system was in
first — the Position Guard judges whether the story confirms the trade, and
if so the position graduates from quick-scalp exits to the normal news-trade
exit rules: no more 15:50 force-flat, overnight allowed via the usual 15:45
decision, wider trailing room. Stops never widen; the safety net never
moves. (2) **MAX TRADES button** in the dashboard header — set your daily
entry cap without touching the terminal. (3) **PERFORMANCE tab** — your
portfolio's total % gain charted against SPY and QQQ.

**When to do this: OUTSIDE market hours** (it restarts the execution
engine). ~15 minutes.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_2-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `src`, `config`, `schema`, `tests`, and `dashboard` folders plus two
   loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** In File
> Explorer, select the five folders (`src`, `config`, `schema`, `tests`,
> `dashboard`) plus the two `.md` files, and drag that selection into the
> GitHub upload box. The upload preview must show paths WITH folders in
> front (e.g. `src/a12_guard/schema.py`) — if you see bare filenames,
> cancel and re-drag. (This is what went wrong on the first v0.12.1
> upload.)

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files** →
   drag the five folders + two `.md` files.
2. **Eight files are REPLACED:**
   - `src/a12_guard/schema.py`, `src/a12_guard/prompt.py`,
     `src/a12_guard/service.py`, `src/a12_guard/context.py`
   - `src/c4_exec/engine.py`, `src/c4_exec/service.py`
   - `config/deadman.yaml`
   - `dashboard/index.html`

   **Four files are NEW:**
   - `schema/migrations/008-promotion.sql`
   - `tests/unit/test_promotion.py`
   - `patch-notes-v0_12_2.md`, `v0_12_2-deploy-guide.md`
3. Commit message: `v0.12.2: promotion rule + max-trades button + performance tab`
4. **Commit changes**, open the commit, confirm **12 changed files**
   (8 replaced + 4 new) with proper folder paths. Different — stop and
   tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.1"` →
   `version = "0.12.2"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.2` → title
   `v0.12.2 — promotion + dashboard` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.2
ls /opt/pipeline/schema/migrations/008-promotion.sql
```

The `ls` must print the path, not "No such file".

## Part 5 — Run the database migration

```bash
sudo -u trader psql "$PIPELINE_DSN" -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/008-promotion.sql
```

Expect `ALTER TABLE` lines + `INSERT` and no ERROR. Confirm:

```bash
sudo -u trader psql "$PIPELINE_DSN" -c "SELECT max(schema_version) FROM journal.schema_meta;"
```

Should print `8`.

## Part 6 — Restart the three touched services

```bash
sudo systemctl restart a12-guard c4-exec c6-dashboard
```

## Part 7 — Check the NAV snapshot timer (feeds the Performance tab)

```bash
systemctl list-timers --all --no-pager | grep -i nav
```

If a `pipeline-nav-snapshot.timer` line appears with a NEXT time — good,
nothing to do. If **nothing appears**, install and start it:

```bash
sudo cp /opt/pipeline/ops/systemd/pipeline-nav-snapshot.service /etc/systemd/system/
sudo cp /opt/pipeline/ops/systemd/pipeline-nav-snapshot.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pipeline-nav-snapshot.timer
```

## Part 8 — Verify

**Dashboard** (hard refresh, Ctrl+Shift+R):
- Header has a **MAX TRADES** button. Click it — the prompt shows the
  current cap; set your higher data-gathering value (needs the kill
  token). A3 applies it on the next signal, no restart.
- A **PERFORMANCE** tab next to HISTORY. Until the nightly snapshot has
  run after your first trade it shows "no NAV history yet" — that's honest,
  not broken. Once rows exist: three lines (Portfolio amber, SPY blue,
  QQQ green), hover for exact values, "data table" expands underneath.

**Promotion** (whenever it first happens — needs a scanner trade AND its
causal news printing while the position is open):

```bash
sudo journalctl -u c4-exec --since today --no-pager | grep -i promoted
sudo -u trader psql "$PIPELINE_DSN" -c "SELECT position_id, ts, detail FROM journal.position_events WHERE event_type='PROMOTED' ORDER BY ts DESC LIMIT 5;"
```

A promoted trade stops appearing in the 15:50 force-flat and instead gets
the normal 15:45 overnight decision. Its stop does not move on promotion —
only the exit *rules* change.

**Guard sanity (first scanner-position news):** the guard verdict line in
the tape now carries the confirmation judgment; a REJECT storm in
`journalctl -u a12-guard` would mean the model is struggling with the new
field — paste it to Claude if you see one.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.1b
sudo systemctl restart a12-guard c4-exec c6-dashboard
```

Migration 008 can stay. To turn off only the promotion behavior without
rolling back: edit `config/deadman.yaml` → `promotion_enabled: false` →
`sudo systemctl restart c4-exec`.

## What comes next

Your list of further C6 tweaks (v0.12.3), plus A11 origin attribution —
scanner vs news vs promoted win rates, MAE/MFE, time-stop hit rate — once
there's a week of journal data worth attributing.
