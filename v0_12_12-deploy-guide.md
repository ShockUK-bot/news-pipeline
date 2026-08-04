# Deploy Guide — v0.12.12 (GATE LAB dashboard tab)

**What this release does:** everything the last two releases measure —
the gate veto counterfactuals and the extended-hours shadow results — is
now on the dashboard in a new **GATE LAB** tab, so you never need psql to
see them. Full story in `patch-notes-v0_12_12.md`.

> Deploy AFTER v0.12.10 and v0.12.11 (this tab reads their tables). The
> order for all three tomorrow: v0.12.10 → v0.12.11 → v0.12.12.

**When to do this: any time.** ~5 minutes. Only the dashboard restarts —
trading services are untouched.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_12-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   one folder (`dashboard`) and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDER itself, not its contents.** The preview must show
> `dashboard/app.py` and `dashboard/index.html` — with the folder in
> front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `dashboard` folder and the two `.md` files.
2. **Two files are REPLACED:** `dashboard/app.py`,
   `dashboard/index.html`. **Two are NEW:** the patch notes and this
   guide.
3. Commit message: `v0.12.12: GATE LAB dashboard tab`
4. **Commit changes**, open the commit, confirm **4 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.11"` →
   `version = "0.12.12"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.12` → title
   `v0.12.12 — GATE LAB tab` → **Publish**.

## Part 4 — Pull and restart the dashboard

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.12
sudo systemctl restart c6-dashboard
```

## Part 5 — Verify

1. Open the dashboard in your browser as usual. Next to LIVE / HISTORY /
   PERFORMANCE there is a new **GATE LAB** tab.
2. Click it. Right after deploy (before the market has done anything)
   the panels politely say "no gate decisions yet today" / "no measured
   vetoes yet" — that's correct, not broken.
3. Service check:

```bash
systemctl is-active c6-dashboard
```

`active`.

## Part 6 — Reboot survival (standing check)

```bash
systemctl is-enabled c6-dashboard
```

`enabled`.

## What you'll see tomorrow

- **During the session:** "Today at the gate" fills with actions and, if
  the re-check window is doing its job, average minutes well above 6.
- **~30 min after each close:** the counterfactual panels fill in — what
  every veto cost or saved, and how each shadow would-trade played out.
- The tab refreshes itself every 30 seconds while open.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.11
sudo systemctl restart c6-dashboard
```
