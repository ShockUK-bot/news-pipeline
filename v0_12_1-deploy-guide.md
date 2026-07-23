# Deploy Guide — v0.12.1 (momentum scanner — trading live)

**What this release does:** adds the momentum scanner — the "catch MU-type
moves that have no news" input from the spec you approved. From the next
session after deploy, the system watches the whole market for big movers on
unusual volume, sends the best few to the analyst, and (if the analyst and
every gate agree) trades them as quick scalps: half normal size, tight
stops, out within the hour if the move stalls, **always flat by 15:50 ET —
never held overnight**. Anti-overtrading caps are layered everywhere; a
normal day produces 0–2 scanner trades, often zero, and the dashboard gets
a scanner panel with an on/off switch.

**When to do this: OUTSIDE market hours** (evening or before 09:30 ET).
~20 minutes. This release touches the execution engine, so don't deploy
mid-session.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_1-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `src`, `config`, `tests`, `schema`, `ops`, and `dashboard` folders plus
   two loose `.md` files.

## Part 2 — Upload to GitHub (browser, same as always)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in ALL SIX folders (**src**, **config**, **tests**, **schema**,
   **ops**, **dashboard**) and the two **`.md`** files.
3. **Fifteen files are REPLACED** (GitHub handles this automatically):
   - `config/exit_profiles.yaml`, `config/gate.yaml`, `config/risk.yaml`
   - `src/a1_triage/service.py`
   - `src/a2_analyst/service.py`, `src/a2_analyst/prompt.py`,
     `src/a2_analyst/schema.py`
   - `src/c3_gate/rules.py`, `src/c3_gate/service.py`
   - `src/a3_risk/service.py`
   - `src/c4_exec/service.py`, `src/c4_exec/state.py`,
     `src/c4_exec/engine.py`, `src/c4_exec/exits.py`
   - `dashboard/app.py`, `dashboard/index.html`

   (that's 16 — the count GitHub shows for replaced files)

   **Eleven files are NEW:**
   - `src/c10_scanner/__init__.py`, `src/c10_scanner/screener.py`,
     `src/c10_scanner/rules.py`, `src/c10_scanner/service.py`
   - `config/scanner.yaml`
   - `ops/systemd/c10-scanner.service`
   - `schema/migrations/007-scanner.sql`
   - `tests/unit/test_scanner.py`, `tests/unit/test_scalp_exits.py`
   - `patch-notes-v0_12_1.md`, `v0_12_1-deploy-guide.md`
4. Commit message: `v0.12.1: C10 momentum scanner + scalp lane, trading live`
5. **Commit changes**, then open the commit and confirm it shows **27
   changed files** (16 replaced + 11 new). Different number — stop and
   tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.0"` →
   `version = "0.12.1"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.1` → title
   `v0.12.1 — momentum scanner` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.1
```

## Part 5 — Run the database migration

```bash
sudo -u trader psql "$PIPELINE_DSN" -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/007-scanner.sql
```

Expect `CREATE TABLE`, `CREATE INDEX`, `INSERT`, a few `ALTER TABLE` lines
and **no ERROR**. (It's wrapped in one transaction — an error means nothing
was applied; paste it to Claude.)

## Part 6 — Install and start the new scanner service

The scanner is a new background service, so it needs a one-time install:

```bash
sudo cp /opt/pipeline/ops/systemd/c10-scanner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now c10-scanner
```

Check it came up:

```bash
sudo systemctl status c10-scanner --no-pager | head -5
```

You want `active (running)`. (Outside 09:50–15:15 ET it idles — that's
correct, it only works during the session.)

## Part 7 — Restart the six touched services

```bash
sudo systemctl restart a1-triage a2-analyst c3-gate a3-risk c4-exec c6-dashboard
```

Note `c4-exec`: if it was manually started before (it's been `disabled` for
boot), restart still works; this doesn't change its enable state.

## Part 8 — Verify (tonight)

**Dashboard:** hard refresh (Ctrl+Shift+R). The LIVE tab now has a
**Momentum scanner** panel with a green **SCANNER ON** button and "no
candidates seen today". The Pipeline load panel shows a new `A1 · scanner`
row.

**Scanner log:**

```bash
sudo journalctl -u c10-scanner -n 20 --no-pager
```

Expect `C10 up` with the window and caps, and nothing else off-hours.

**Tests (optional):**

```bash
cd /opt/pipeline && sudo -u trader env PYTHONPATH=src PIPELINE_DSN="$PIPELINE_DSN_TEST" /opt/pipeline/.venv/bin/python -m pytest tests/unit/test_scanner.py tests/unit/test_scalp_exits.py -q
```

Expect `47 passed`.

## Part 9 — First-session watch checklist (tomorrow)

The scanner trades from day one, so give the first session a few glances:

1. **~10:00 ET** — scanner panel starts showing candidates (mostly
   FILTERED; that's the design working, not a problem).
2. **Any EMITTED candidate** — within ~2 minutes you should see the chain
   on the decision tape: TRIAGE ESCALATE → ANALYST THESIS (or REJECT —
   also fine) → GATE PASS or a SCANNER_* veto → RISK SIZE or veto.
3. **If a scanner trade opens** — it shows a SCAN chip in Open positions,
   at roughly HALF the size of a comparable news trade.
4. **15:50 ET with a scanner position open** — it must exit within a
   minute or two, journaled FORCE_FLAT:

   ```bash
   sudo journalctl -u c4-exec --since "15:45" --no-pager | grep -i "force flat"
   ```

5. **16:05 ET** — confirm no scanner position survived:

   ```bash
   sudo -u trader psql "$PIPELINE_DSN" -c "SELECT ticker, origin, status FROM journal.positions WHERE origin='scanner' AND status='OPEN';"
   ```

   **Zero rows expected.** A row here means force-flat failed — tell
   Claude immediately (the broker-resident catastrophe stop still protects
   the position overnight regardless).
6. **Any time** — too noisy? Click **SCANNER ON** to turn it off (needs
   the kill token). News trading is completely unaffected.

## Rollback (if anything misbehaves)

Fastest, no redeploy: click **SCANNER ON → off** on the dashboard — the
input stops, everything else keeps running on v0.12.1 code.

Full rollback:

```bash
sudo systemctl stop c10-scanner
sudo systemctl disable c10-scanner
sudo -u trader git -C /opt/pipeline checkout v0.12.0
sudo systemctl restart a1-triage a2-analyst c3-gate a3-risk c4-exec c6-dashboard
```

Migration 007 can stay — it's additive and v0.12.0 code never reads it.

## What comes next

v0.12.2 (small): A11 attribution split by origin (scanner win rate, MAE/MFE,
time-stop hit rate as first-class weekly metrics), A7/A8 report lines, and
any threshold tuning the first week's journal evidence suggests via A9.
