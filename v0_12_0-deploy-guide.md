# Deploy Guide — v0.12.0 (TA context + trade-origin dashboard columns)

**What this release does:** two things, both additive. First, the analyst
(A2) and position guard (A12) now receive real technical-analysis numbers —
RSI, VWAP distance, relative volume, trend, distance from the 52-week
high — computed by code from market data you already pay for, so "is this
already priced in?" gets answered against evidence instead of instinct.
Second, the dashboard now shows for every trade whether it came from **news**
or the **scanner** (the scanner itself arrives in v0.12.1 — until then
everything correctly shows NEWS), and the Open positions table gains three
new columns: **Origin**, **Cost** (what the position cost to buy), and
**P/L %**. This release does NOT change any gating, sizing, or exit
behavior — it is pure information.

**When to do this: any time, including during market hours.** ~15 minutes.
One small database migration (safe, additive, takes under a second).

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_0-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a
   folder `v0_12_0-pack` containing `src`, `tests`, `schema`, and
   `dashboard` folders plus two loose `.md` files.

## Part 2 — Upload to GitHub (browser, same as always)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in, from the extracted `v0_12_0-pack` folder: the **src** folder,
   the **tests** folder, the **schema** folder, the **dashboard** folder,
   and the two **`.md`** files.
3. **Six files are REPLACED** (GitHub handles this automatically):
   - `src/common/clock.py`
   - `src/a2_analyst/context.py`
   - `src/a2_analyst/prompt.py`
   - `src/a12_guard/context.py`
   - `dashboard/app.py`
   - `dashboard/index.html`

   **Five files are NEW:**
   - `src/common/ta.py`
   - `tests/unit/test_ta.py`
   - `schema/migrations/006-position-origin.sql`
   - `patch-notes-v0_12_0.md`
   - `v0_12_0-deploy-guide.md`
4. Commit message: `v0.12.0: TA context pack + trade-origin dashboard columns`
5. **Commit changes**, then open the commit and confirm it shows **11
   changed files** (6 replaced + 5 new). If it shows anything different —
   stop and tell Claude before going further.

## Part 3 — Version bump + release

1. Open `pyproject.toml` in the repo → pencil (edit) icon → change
   `version = "0.11.12"` to `version = "0.12.0"` → **Commit changes** to
   `main`.
2. **Releases → Draft a new release** → tag `v0.12.0` → title
   `v0.12.0 — TA context + trade origin` → **Publish**.

## Part 4 — Pull onto the Spark

Open a terminal on the Spark and run these one at a time:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.0
```

## Part 5 — Run the database migration

This adds the `origin` column and the new dashboard view. It is additive
and safe with the system running. One command:

```bash
sudo -u trader psql "$PIPELINE_DSN" -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/006-position-origin.sql
```

You should see a few `ALTER TABLE` / `CREATE VIEW` / `INSERT` lines and
**no line starting with `ERROR`**. If you see an ERROR, stop and paste it
to Claude — nothing has been half-applied (the migration wraps itself in a
transaction).

## Part 6 — Restart the three affected services

```bash
sudo systemctl restart a2-analyst
sudo systemctl restart a12-guard
sudo systemctl restart c6-dashboard
```

Nothing else needs a restart — no other service reads the changed files.

## Part 7 — Verify

**Dashboard (right away):** open the C6 console in your browser and press
Ctrl+Shift+R (hard refresh, so the new page isn't served from cache). The
Open positions table should now show **Origin / Cost / P/L %** columns.
Any open position shows a blue **NEWS** chip, Cost in dollars, and a green
or red percentage. The HISTORY tab's Closed trades table also has an
Origin column.

**Analyst context (next time A2 runs):** the TA pack only logs when
something is wrong, so a quiet log is a healthy log:

```bash
sudo journalctl -u a2-analyst --since "1 hour ago" --no-pager | grep -i "ta pack\|ta snapshot\|ta daily\|ta minute"
```

Seeing **nothing** is good. If lines appear saying `ta ... unavailable`,
the pack is degrading to nulls (the system keeps working; the analyst just
sees "unavailable") — paste them to Claude if they persist. The positive
check arrives on its own: upcoming A2 decisions' `reason` text on the
dashboard tape will start citing RSI / VWAP / relative volume when they
matter to the call.

**Tests (optional but nice):**

```bash
cd /opt/pipeline && sudo -u trader env PYTHONPATH=src PIPELINE_DSN="$PIPELINE_DSN_TEST" python -m pytest tests/unit/test_ta.py -q
```

Expect `16 passed`.

## Rollback (if anything misbehaves)

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.12
sudo systemctl restart a2-analyst a12-guard c6-dashboard
```

The migration does NOT need to be undone — it's additive; v0.11.12 code
never references the new column and the old dashboard ignores the view's
extra columns.

## What comes next

v0.12.1 is the scanner itself: the `c10-scanner` service that watches for
big no-news movers (the MU case), the scanner-specific gate profile, the
`scalp_v1` quick-exit profile with the hard 15:50 ET force-flat, and all
the anti-overtrading caps — trading from day one at half size per your
decision. That release WILL touch execution, so its guide will include a
first-session watch checklist.
