# Deploy Guide — v0.12.3 (C6 console tweaks)

**What this release does:** the three dashboard fixes you asked for — the
decision tape now fills its whole tile instead of stopping at a fixed
height, the CHAT/PERFORMANCE tab overlap bug is fixed (each tab now fully
replaces the others), and the LIVE tab is rearranged: Momentum scanner
directly below Open positions, and Vetoed trades above System health.

**When to do this: any time, including market hours.** ~5 minutes. Only the
dashboard restarts — no trading service is touched.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_3-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get one
   `dashboard` folder and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDER itself, not its contents.** Select the `dashboard`
> folder plus the two `.md` files and drag that selection into the upload
> box. The preview must show `dashboard/index.html` and
> `dashboard/app_chat.py` — with the folder in front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files** →
   drag the `dashboard` folder + the two `.md` files.
2. **Two files are REPLACED:** `dashboard/index.html`,
   `dashboard/app_chat.py`. **Two are NEW:** the patch notes and this
   guide.
3. Commit message: `v0.12.3: C6 tweaks — tape fill, tab fix, tile order`
4. Commit, open the commit, confirm **4 changed files** with folder paths.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.2"` →
   `version = "0.12.3"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.3` → title
   `v0.12.3 — C6 console tweaks` → **Publish**.

## Part 4 — Pull and restart (one service)

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.3
sudo systemctl restart c6-dashboard
```

(The usual harmless "channel 3: open failed" lines may appear while your
SSH tunnel reconnects to the restarted dashboard.)

## Part 5 — Verify (browser, hard refresh Ctrl+Shift+R)

1. LIVE tab order: Open positions → Momentum scanner → Decision tape with
   Vetoed trades (top) / System health (bottom) beside it → Pipeline load.
2. The decision tape reaches the full height of its tile — no dead space
   below it; it scrolls inside itself once it's fuller than the tile.
3. The bug sequence: click **PERFORMANCE**, then **CHAT** — only the chat
   shows and only CHAT is underlined. Click back to **PERFORMANCE** — only
   the chart shows. Try LIVE and HISTORY too; every tab should fully
   replace the previous one.
4. Decision-tape latencies read in seconds ("12.5s"), not milliseconds.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.2
sudo systemctl restart c6-dashboard
```
