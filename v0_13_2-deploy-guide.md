# Deploy Guide — v0.13.2 (dashboard hotfix 2: origin column back)

**What this release does:** restores the three dashboard columns that
v0.13.0's migration accidentally dropped from the positions view — origin
(news | scanner | thesis), total cost, and % P&L — and makes % P&L read
correctly for SHORT positions (a winning short now shows green, not red).

Same shape as v0.13.1: one migration, one dashboard restart, **nothing
trading-related is touched**. About 10 minutes, safe while live.

If anything in Part 4 or 5 prints something you don't expect, **stop and
paste it to Claude**.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_13_2-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a
   `schema` folder and two loose `.md` files.

## Part 2 — Upload to GitHub (browser, same as always)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload
   files**.
2. Drag in: the **schema** folder and the two **`.md`** files.
3. **All 3 files are NEW** (nothing is replaced):
   - `schema/migrations/015-restore-dash-view.sql`
   - `patch-notes-v0_13_2.md`
   - `v0_13_2-deploy-guide.md`
4. Commit message: `v0.13.2: dashboard hotfix 2 — restore
   origin/total_cost/pct_pnl, side-aware pct_pnl`
5. **Commit changes**, then confirm the commit shows **3 changed files**.

## Part 3 — Version bump + release

1. Open `pyproject.toml` → pencil (edit) icon → change
   `version = "0.13.1"` to `version = "0.13.2"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.13.2` → title
   `v0.13.2 — dashboard hotfix 2` → **Publish**.

## Part 4 — Pull onto the Spark, run the fix, restart the dashboard

One at a time:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.2
```

("local changes would be overwritten" → stop, paste to Claude.)

The migration — test database first, then live:

```bash
sudo -u postgres psql -d trading_test -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/015-restore-dash-view.sql
```

```bash
sudo -u postgres psql -d trading -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/015-restore-dash-view.sql
```

Each ends with `COMMIT`. Any ERROR — stop, paste to Claude.

```bash
sudo systemctl restart c6-dashboard
```

```bash
systemctl is-active c6-dashboard
```

## Part 5 — Confirm it worked

1. Refresh the dashboard. The positions panel should show origin, total
   cost and % P&L again (SNDK-era closed rows too).
2. Database check — this lists the view's columns; the last four should
   be `origin, total_cost, pct_pnl, side`:

```bash
sudo -u postgres psql -d trading -c "
SELECT string_agg(column_name, ', ' ORDER BY ordinal_position)
FROM information_schema.columns
WHERE table_schema='journal' AND table_name='dash_positions';"
```

Expected:

```
id, ticker, qty, entry_price, current_price, stop_price, target_price, opened_ts, closed_ts, status, exit_reason, realized_pnl, thesis, item_id, origin, total_cost, pct_pnl, side
```

3. Owner + version:

```bash
sudo -u postgres psql -d trading -c "
SELECT (SELECT viewowner FROM pg_views WHERE schemaname='journal' AND viewname='dash_positions') AS owner,
       (SELECT max(schema_version) FROM journal.schema_meta) AS schema_v;"
```

Expected: `trader` and **15**.

Still wrong after all that? `sudo journalctl -u c6-dashboard -n 30
--no-pager` → paste to Claude.

## Rollback

None needed — this only restores columns the dashboard was already built
to read, plus the `side` column at the end.
