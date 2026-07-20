# Deploy Guide — v0.11.6b (fold chat mount into app.py + finish the v0.11.6 deploy)

**What this does:** a one-file follow-up that puts the A13 chat-router line back
into `app.py` (so the v0.11.6 checkout stops being blocked by your local edit),
then finishes turning on the v0.11.6 dashboard panel and the prune timer.

**Safe any time.** No trading-logic, DB, or model changes.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_11_6b-pack.zip` from the chat.
2. Extract it. You'll get a `v0_11_6b-pack` folder with one `dashboard` folder
   and two `.md` files.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **dashboard** folder and the two `.md` files.
3. **One file is REPLACED:** `dashboard/app.py`.
4. Commit message: `v0.11.6b: fold A13 chat-router mount into app.py`
5. Commit, then confirm **3 changed files** (1 replaced + 2 new `.md`).

## Part 3 — Version bump + release

1. `pyproject.toml` → change `version = "0.11.6"` to `version = "0.11.6b"` →
   commit to `main`.
2. **Releases → Draft a new release** → tag `v0.11.6b` → title
   `v0.11.6b — chat mount in app.py` → **Publish**.

## Part 4 — Pull onto the Spark (clears the block)

The local edit is now captured in v0.11.6b, so it is safe to discard it:

```bash
sudo -u trader git -C /opt/pipeline checkout -- dashboard/app.py
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.6b
```

This should succeed with no "local changes" error. If a *different* file is
still named, stop and tell Claude.

Verify (you want `v0.11.6b`, `1`, `2`):

```bash
sudo -u trader git -C /opt/pipeline describe --tags            # v0.11.6b
grep -c "Pipeline load" /opt/pipeline/dashboard/index.html     # 1  (panel)
grep -c "make_chat_router" /opt/pipeline/dashboard/app.py      # 2  (chat restored)
```

## Part 5 — Restart the dashboard

```bash
sudo systemctl restart c6-dashboard
sudo systemctl status c6-dashboard --no-pager
```

Look for `Active: active (running)`. Then open the dashboard and **hard-refresh**
(`Ctrl+Shift+R`, or `Cmd+Shift+R` on Mac). You should see:
- a new **Pipeline load** panel at the bottom of the LIVE tab, and
- your **CHAT** tab still present and working.

## Part 6 — Install the prune timer (if not already done)

```bash
sudo cp /opt/pipeline/ops/systemd/queue-prune.service /etc/systemd/system/
sudo cp /opt/pipeline/ops/systemd/queue-prune.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now queue-prune.timer
```

Prove it once and confirm it reached the dashboard's health panel:

```bash
sudo systemctl start queue-prune.service
sudo journalctl -u queue-prune -n 10 --no-pager
systemctl list-timers queue-prune.timer --no-pager
psql "$PIPELINE_DSN" -c "SELECT component, status, detail FROM journal.health WHERE component='queue_prune';"
```

You want a `queue prune OK: deleted N ...` line, a future `NEXT` on the timer,
and a `queue_prune` row in health. (Retention defaults to 7 days; set
`QUEUE_PRUNE_KEEP_DAYS=<n>` in `/etc/pipeline/pipeline.env` to change it.)

## Part 7 — Done

Dashboard shows the Pipeline load panel and the CHAT tab; the prune timer is
scheduled. That completes v0.11.6 + v0.11.6b.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.6
sudo systemctl restart c6-dashboard
sudo systemctl disable --now queue-prune.timer   # only if you want to stop pruning
```
