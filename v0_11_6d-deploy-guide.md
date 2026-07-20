# Deploy Guide — v0.11.6d (batch-aware Pipeline load thresholds)

**What this does:** stops the two *scheduled* queues (`signal.overnight`,
`signal.thesis`) on the dashboard's Pipeline load panel from showing red/amber
every day. They're drained by daily jobs (A4 at 07:00 ET, A5 at 21:30 ET), so a
big backlog between runs is normal — after this they only alarm if a drain is
actually missed.

**Dashboard-only. No service restart** — `index.html` is read fresh on every
page load, so a browser hard-refresh is all that's needed at the end.

---

## Part 1 — Get the pack

1. Download `v0_11_6d-pack.zip`, extract it. You'll get a `dashboard` folder and
   two `.md` files.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **dashboard** folder and the two `.md` files.
3. **One file is REPLACED:** `dashboard/index.html`.
4. Commit message: `v0.11.6d: batch-aware pipeline-load thresholds`
5. Commit; confirm **3 changed files** (1 replaced + 2 new `.md`).

## Part 3 — Version bump + release

1. `pyproject.toml` → change `version = "0.11.6c"` to `version = "0.11.6d"` →
   commit to `main`.
2. **Releases → Draft a new release** → tag `v0.11.6d` → title
   `v0.11.6d — batch-aware load thresholds` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.6d
```

**No restart needed.** Just open the dashboard and **hard-refresh**
(`Ctrl+Shift+R`).

## Part 5 — Confirm

On the LIVE tab's Pipeline load panel, `signal.overnight` and `signal.thesis`
should now show green with a small **· scheduled** tag next to the queue name
(instead of red/amber), while any genuinely busy real-time queue still colours.

Verify the file landed:

```bash
grep -c "scheduled" /opt/pipeline/dashboard/index.html   # >= 1
```

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.6c
```

Then hard-refresh. No other changes.
