# Deploy Guide — v0.11.1 (RSS hotfix)

**What this release does:** stops one flaky news feed (PR Newswire) from
turning your whole "RSS" line on the dashboard yellow when the other two
feeds are working fine, and gives that one feed a better chance of
actually working again. This is a small, one-file fix — no database
changes, no new timers, no new permissions, no new keys.

**When to do this:** any time, doesn't need to wait for market close.
~5 minutes. Builds on v0.11.0 — deploy that first if you haven't.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_11_1-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `v0_11_1-pack` containing one `src` folder and the two loose `.md`
   files.

## Part 2 — Upload to GitHub (browser, same as before)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload
   files**.
2. Drag in the **src** folder and the two `.md` files from
   `v0_11_1-pack`.
3. **One file is REPLACED this time** (GitHub handles it automatically):
   `src/c1_ingestion/sources/rss.py`.
4. Commit message: `v0.11.1: RSS hotfix — per-feed health + browser User-Agent`
5. **Commit changes**, then open the commit and confirm **3 changed
   files** (1 replaced + 2 new `.md` files). Anything different → stop,
   tell Claude.

## Part 3 — Version bump + release

1. Open `pyproject.toml` in the repo → pencil (edit) icon → change
   `version = "0.11.0"` to `version = "0.11.1"` → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.11.1` → title
   `v0.11.1 — RSS hotfix` → **Publish**.

## Part 4 — Pull onto the Spark and restart ingestion

Open a terminal on the Spark and run these one at a time:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.1
sudo systemctl restart c1-ingestion
```

Then check it came back up cleanly:

```bash
sudo systemctl status c1-ingestion --no-pager
```

Look for `Active: active (running)` near the top. If it instead says
`failed` or keeps restarting, stop and copy the last 20 lines of
`sudo journalctl -u c1-ingestion -n 20 --no-pager` to Claude.

## Part 5 — Confirm on the dashboard

Give it about a minute, then open the C6 dashboard's health panel:

- You should now see a separate line for each RSS feed —
  `ingestion:rss:prnewswire-news`, `ingestion:rss:globenewswire-public`,
  `ingestion:rss:businesswire-all` — alongside the existing aggregate
  `ingestion:rss` row.
- If the User-Agent change worked, `ingestion:rss:prnewswire-news` turns
  green (OK) within a couple of minutes.
- If it's still yellow after a day or two, that's fine to ignore for now
  — the aggregate `ingestion:rss` row will stay green as long as the
  other two feeds keep working, which is the whole point of this fix. Tell
  Claude if you want to just drop that one feed at that point.

## Rollback (if anything misbehaves)

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.0
sudo systemctl restart c1-ingestion
```

Nothing else to undo — no migrations, no timers were touched.
