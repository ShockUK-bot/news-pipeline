# Deploy Guide — v0.11.4 (Decision tape: show full detail text)

**What this release does:** the "Decision tape" panel on the C6 dashboard
(LIVE tab) was cutting every reason off at 70 characters. This patch removes
that cutoff so you can see the model's full reasoning for each row. It's a
single frontend file — no database changes, no new services, no restart of
any pipeline process. This is safe to do any time, including during market
hours.

**When to do this:** any time. ~5 minutes, and you don't even need to
restart anything on the Spark — just a browser refresh at the end.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_11_4-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a
   `v0_11_4-pack` folder containing one `dashboard` folder and two loose
   `.md` files.

## Part 2 — Upload to GitHub (browser)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload
   files**.
2. Drag in the **dashboard** folder and the two `.md` files from
   `v0_11_4-pack`. (Drag the folder itself, not the files inside it
   individually — dragging the folder keeps the `dashboard/index.html`
   path intact so GitHub knows to replace the existing file rather than
   create a new one somewhere else.)
3. **One file is REPLACED this time** (GitHub handles it automatically):
   `dashboard/index.html`.
4. Commit message: `v0.11.4: decision tape shows full detail text`
5. **Commit changes**, then open the commit and confirm **3 changed
   files** (1 replaced + 2 new `.md` files). Anything different → stop,
   tell Claude before continuing.

## Part 3 — Version bump + release

1. Open `pyproject.toml` in the repo → pencil (edit) icon → change
   `version = "0.11.3"` to `version = "0.11.4"` → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.11.4` → title
   `v0.11.4 — Decision tape full detail text` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.4
```

That's it — **no `systemctl restart` needed for this one.** The dashboard
reads `index.html` off disk fresh on every page load, so as soon as the new
file is on the Spark, the next time anyone opens (or reloads) the dashboard
in a browser they'll get the new version. (If you want to double check the
file actually landed, `grep -c "slice(0,70)" /opt/pipeline/dashboard/index.html`
should print `0`.)

## Part 5 — Confirm it worked

1. Open the dashboard in your browser (or switch to the tab if it's already
   open) and do a **hard refresh** — `Ctrl+Shift+R` on Windows/Linux,
   `Cmd+Shift+R` on Mac. This matters: a normal refresh can reuse the old
   cached page.
2. Look at the LIVE tab's **Decision tape** panel. Rows with a short reason
   look the same as before. Rows with a longer one (ANALYST/THESIS rows
   tend to have the longest reasoning text) should now wrap onto a second
   line under the timestamp/chip/ticker/action row instead of cutting off
   with the text just stopping mid-word.
3. If it still looks cut off after a hard refresh, tell Claude — that
   usually means either the file didn't actually land on the Spark (recheck
   Part 4's `grep`) or the browser is still serving a cached copy from
   somewhere unusual (e.g. a proxy).

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.3
```

Then hard-refresh the dashboard in your browser again. Nothing else to
undo — no database or service changes were made.
