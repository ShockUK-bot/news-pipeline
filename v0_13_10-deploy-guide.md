# Deploy Guide — v0.13.10 (dashboard Side column)

**What this release does:** the Open Positions table gains a **Side**
column — green `LONG` chip or red `SHORT` chip — and the row's dollar
P&L now reads correctly for shorts (a winning short shows green; before
this it showed a red dollar figure next to a green percentage).

Smallest release yet: one file, one dashboard restart, nothing
trading-related touched. ~5 minutes, safe while live.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_13_10-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a
   `dashboard` folder and two loose `.md` files.

## Part 2 — Upload to GitHub (browser, same as always)

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in: the **dashboard** folder and the two **`.md`** files.
3. **1 file is REPLACED:** `dashboard/index.html`
   **2 files are NEW:** `patch-notes-v0_13_10.md`,
   `v0_13_10-deploy-guide.md`
4. Commit message: `v0.13.10: dashboard Side column + side-aware row P&L`
5. Confirm the commit shows **3 changed files**.

## Part 3 — Version bump + release

1. `pyproject.toml` → edit → `version = "0.13.9"` → `"0.13.10"` → commit.
2. **Releases → Draft a new release** → tag `v0.13.10` → title
   `v0.13.10 — dashboard Side column` → **Publish**.

## Part 4 — Pull onto the Spark and restart the dashboard

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.10
```

("local changes would be overwritten" → stop, paste to Claude.)

```bash
sudo systemctl restart c6-dashboard
```

```bash
systemctl is-active c6-dashboard
```

## Part 5 — Confirm it worked

Hard-refresh the dashboard page (**Ctrl+Shift+R** — the browser caches
the old page otherwise). The Open Positions table now has a **Side**
column after Ticker. Any currently open long shows a green `LONG` chip.
When the first short opens you'll see the red `SHORT` chip — and its
dollar P&L and percentage will agree in color.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.9
sudo systemctl restart c6-dashboard
```

(Restores the missing column and the wrong-sign dollar P&L on shorts.
No reason to.)
