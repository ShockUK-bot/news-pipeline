# Deploy Guide — v0.11.6 (Queue-prune job + Pipeline-load dashboard panel)

**What this release does:** two safe, additive extras from today's incident
review — (1) a nightly job that trims the completed-message rows out of the
queue table so it can't bloat, and (2) a new **Pipeline load** panel on the
dashboard's LIVE tab that shows, at a glance, whether the analyst or any other
agent is backing up (with a red/amber warning and a repeat-analysis watch that
would have flagged today's YUM loop instantly).

**When to do this:** any time, including market hours. No agent, model, or
trading code changes. ~10–15 minutes.

There are two pieces to install: the **dashboard** (a service restart) and the
**prune timer** (a couple of `sudo` commands). Both are below.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_11_6-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get a
   `v0_11_6-pack` folder containing `ops`, `dashboard`, and `tests` folders and
   two loose `.md` files.

## Part 2 — Upload to GitHub (browser)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in **all five** items from `v0_11_6-pack`: the **ops**, **dashboard**,
   and **tests** folders, and the two `.md` files. (Drag the folders
   themselves so the paths stay intact.)
3. **Three files are REPLACED** (GitHub handles it automatically):
   `dashboard/app.py`, `dashboard/index.html`,
   `tests/integration/test_dashboard.py`. The rest are new.
4. Commit message: `v0.11.6: queue-prune job + pipeline-load dashboard panel`
5. **Commit changes**, then open the commit and confirm **8 changed files**
   (3 replaced + 5 new: 3 ops files + 2 `.md`). Anything different → stop and
   tell Claude.

## Part 3 — Version bump + release

1. Open `pyproject.toml` → pencil (edit) → change `version = "0.11.5"` to
   `version = "0.11.6"` → commit to `main`.
   - If it doesn't say `0.11.5`, stop and tell Claude the number you see.
2. **Releases → Draft a new release** → tag `v0.11.6` → title
   `v0.11.6 — Queue prune + pipeline-load panel` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.6
```

## Part 5 — Turn on the dashboard panel

The panel is in `dashboard/`. `index.html` is read fresh on every page load,
but `app.py` (the backend) needs a restart:

```bash
sudo systemctl restart c6-dashboard
sudo systemctl status c6-dashboard --no-pager
```

Look for `Active: active (running)`. If it says `failed`, copy the last 20
lines of `sudo journalctl -u c6-dashboard -n 20 --no-pager` to Claude.

Then open the dashboard and **hard-refresh** (`Ctrl+Shift+R`, or `Cmd+Shift+R`
on Mac). On the LIVE tab you'll see a new **Pipeline load** panel at the
bottom: a row per queue (labelled with the agent that consumes it), with
Ready / In-flight / Oldest-wait columns, and a "Repeat-analysis watch" line
underneath. On a healthy system every row is green with 0s; a backed-up queue
turns amber (≥50 ready) or red (≥200), and any ticker analyzed >3× in 30 min
appears as a chip.

## Part 6 — Install the prune timer

Copy the two unit files into place, load them, and enable the timer:

```bash
sudo cp /opt/pipeline/ops/systemd/queue-prune.service /etc/systemd/system/
sudo cp /opt/pipeline/ops/systemd/queue-prune.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now queue-prune.timer
```

Run it once now to prove it works (safe — it only deletes completed rows older
than 7 days):

```bash
sudo systemctl start queue-prune.service
sudo journalctl -u queue-prune -n 10 --no-pager
```

You should see a line like `queue prune OK: deleted N done rows older than 7d`.
Confirm the timer is scheduled and the result reached the dashboard:

```bash
systemctl list-timers queue-prune.timer --no-pager
psql "$PIPELINE_DSN" -c "SELECT component, status, detail, updated_ts FROM journal.health WHERE component='queue_prune';"
```

The timer should show a `NEXT` run around 03:10, and the health query should
show the `queue_prune` row (it'll also appear in the dashboard's System health
panel).

**Optional — change how long completed rows are kept** (default 7 days): add a
line to `/etc/pipeline/pipeline.env`, e.g. `QUEUE_PRUNE_KEEP_DAYS=14`, then
`sudo systemctl restart` isn't needed — the next timer run picks it up.

## Part 7 — Confirm it worked

- Dashboard LIVE tab shows the **Pipeline load** panel, mostly green 0s on a
  healthy system.
- `systemctl list-timers` lists `queue-prune.timer` with a future `NEXT`.
- `journal.health` has a `queue_prune` row.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.5
sudo systemctl restart c6-dashboard
sudo systemctl disable --now queue-prune.timer   # stop the prune job
```

(Optionally `sudo rm /etc/systemd/system/queue-prune.{service,timer}` and
`sudo systemctl daemon-reload` to remove the unit files entirely.) No database
or trading-config changes were made.
