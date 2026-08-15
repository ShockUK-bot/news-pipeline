# Deploy Guide — v0.13.1 (dashboard hotfix)

**What this release does:** brings the dashboard back. v0.13.0's migration
rebuilt one dashboard view without handing ownership back to the `trader`
role, so the dashboard has shown "disconnected, no data" since deploy day.
This release is a one-statement database fix plus one service restart.
**Trading has been unaffected the whole time.**

**About 10 minutes. Safe to run while the system is live** — nothing
trading-related restarts.

If anything in Part 4 or 5 prints something you don't expect, **stop and
paste it to Claude**.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_13_1-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a
   `schema` folder and two loose `.md` files.

## Part 2 — Upload to GitHub (browser, same as always)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload
   files**.
2. Drag in: the **schema** folder and the two **`.md`** files.
3. **All 3 files are NEW** (nothing is replaced):
   - `schema/migrations/014-dashboard-view-owner.sql`
   - `patch-notes-v0_13_1.md`
   - `v0_13_1-deploy-guide.md`
4. Commit message: `v0.13.1: dashboard hotfix — dash_positions owner`
5. **Commit changes**, then open the commit and confirm it shows **3
   changed files**. Anything different — stop and tell Claude.

## Part 3 — Version bump + release

1. Open `pyproject.toml` in the repo → pencil (edit) icon → change
   `version = "0.13.0"` to `version = "0.13.1"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.13.1` → title
   `v0.13.1 — dashboard hotfix` → **Publish**.

## Part 4 — Pull onto the Spark, run the fix, restart the dashboard

Open a terminal on the Spark and run these one at a time:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.1
```

If that prints "local changes would be overwritten", **stop** and paste it
to Claude.

Now the migration — this IS the fix. Test database first, then live:

```bash
sudo -u postgres psql -d trading_test -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/014-dashboard-view-owner.sql
```

```bash
sudo -u postgres psql -d trading -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/014-dashboard-view-owner.sql
```

Each should end with `COMMIT`. Any ERROR line — stop, paste it to Claude.

Restart the dashboard (the ONLY service this release touches):

```bash
sudo systemctl restart c6-dashboard
```

```bash
systemctl is-active c6-dashboard
```

Should print `active`.

## Part 5 — Confirm it worked

1. Open the dashboard in your browser and refresh. It should reconnect and
   show positions, decisions, and health again within a few seconds.
2. Confirm the database agrees the fix landed:

```bash
sudo -u postgres psql -d trading -c "
SELECT viewowner FROM pg_views
WHERE schemaname='journal' AND viewname='dash_positions';"
```

Expected output: `trader`.

3. And the migration record:

```bash
sudo -u postgres psql -d trading -c "
SELECT max(schema_version) FROM journal.schema_meta;"
```

Expected: **14**.

If the dashboard is STILL blank after all of the above shows the expected
values, run this and paste the output to Claude:

```bash
sudo journalctl -u c6-dashboard -n 30 --no-pager
```

## Rollback

None needed — the migration only restores the ownership the view had
before v0.13.0. There is nothing to roll back to.
