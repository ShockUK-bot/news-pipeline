# Deploy Guide — v0.8.0 (Phase 7: A4 Pre-Market Review)

**What this release does:** every weekday at 6:00 AM Chicago (7:00 ET), the
system reads everything that broke overnight and over the weekend, has the
heavy model rank it, emails you a pre-market action sheet, and queues the
best candidates for the analyst to evaluate at 8:45 AM Chicago against live
opening prices — where the existing "already priced into the gap?" rule
decides. Until now all of that news just piled up unread.

**When to do this:** today (Saturday) — then the whole path runs for real
Monday morning. Total time: ~15 minutes. No database changes, no new
passwords, no new permissions.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_8_0-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `v0_8_0-pack` containing `src`, `config`, `ops`, `tests` and the two
   loose `.md` files.

## Part 2 — Upload to GitHub (browser, same as before)

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in **src**, **config**, **ops**, **tests** and the two `.md` files.
   All files are NEW.
3. Commit message: `v0.8.0: Phase 7 — A4 pre-market review + open handoff`
4. **Commit changes**, then open the commit and confirm **12 files added**,
   including `src/a4_premarket/service.py`. Anything missing → stop, tell
   Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil → `version = "0.7.0"` → `version = "0.8.0"` →
   commit to `main`.
2. **Releases → Draft a new release** → tag `v0.8.0` → title
   `v0.8.0 — A4 pre-market review` → **Publish**.

## Part 4 — Pull onto the Spark and test

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.8.0
cd /opt/pipeline
sudo -u trader bash -c 'export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test MARKETDATA=fake BROKER=fake \
  PIPELINE_DSN=postgresql://trader:PASSWORD@127.0.0.1:5432/trading_test && .venv/bin/python -m pytest tests/ -q'
```

Expect **318 passed, 0 failed** (13 more than v0.7.0). Qdrant `.lock`
errors → `sudo rm -rf /tmp/qdrant-*` and re-run.

## Part 5 — Install the timer

```bash
sudo cp /opt/pipeline/ops/systemd/a4-premarket.service /opt/pipeline/ops/systemd/a4-premarket.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now a4-premarket.timer
systemctl list-timers a4-premarket.timer --no-pager
```

NEXT should show the next weekday at 07:00 ET (06:00 your time).

## Part 6 — Smoke test: run this morning's review NOW

First, curiosity check — how much has been piling up unread since Phase 2:

```bash
sudo -u trader psql -d trading -c "SELECT count(*) FROM queue.messages WHERE queue_name='signal.overnight' AND done_ts IS NULL;"
```

Now run a real review (weekend override — this is a genuine run: it drains
the backlog, emails you the sheet, and anything it queues as an open
candidate will be evaluated Monday 9:45 ET by the normal analyst + gate
path, paper account, all limits enforced):

```bash
sudo -u trader env PYTHONPATH=/opt/pipeline/src bash -c 'set -a; source /etc/pipeline/pipeline.env; set +a; cd /opt/pipeline && .venv/bin/python - <<PY
import asyncio
from common.config import config_path, load_yaml
from common.journal import register_config_version
from a4_premarket.service import run_premarket

async def main():
    cfg = load_yaml(config_path("a4.yaml"))
    cfg.setdefault("report", {})["send_on_nonsession"] = True
    await register_config_version("a4 smoke test")
    print("outbox row:", await run_premarket(cfg))

asyncio.run(main())
PY'
```

Allow 5–10 minutes (heavy model load + one ranking call; it stops the heavy
model when done). Then:

```bash
sudo systemctl start c5-mailer.service
```

**Check your inbox** for "Pre-market ..." — the ranked sheet with the
model's overnight summary. Then verify the mechanics:

```bash
sudo -u trader psql -d trading -c "SELECT action, count(*) FROM journal.decisions WHERE stage='PREMARKET' GROUP BY 1;"
sudo -u trader psql -d trading -c "SELECT dedup_key, priority, available_ts AT TIME ZONE 'America/Chicago' AS available_ct FROM queue.messages WHERE queue_name='signal.analyst' AND dedup_key LIKE '%%handoff' AND done_ts IS NULL;"
sudo -u trader psql -d trading -c "SELECT count(*) FROM queue.messages WHERE queue_name='signal.overnight' AND done_ts IS NULL;"
```

Expect: an `EXPIRED_BULK` row (big count — the old backlog), some mix of
`OPEN_CANDIDATE` / `THESIS` / `IGNORE`, any handoff rows showing
`available_ct` of **Monday 08:45** (that's 9:45 ET), and overnight
queue depth **0**.

## Part 7 — Monday's rhythm (all automatic now)

- **06:00 CT** — A4 reviews the night, action sheet hits your inbox ~06:30
- **08:30 CT** — market opens; SIP data live
- **08:45 CT** — queued open candidates reach A2, open-handoff gate decides
- **All day** — A12 guards any position the system takes
- **15:35 CT** — A7's EOD report lands in your inbox

## Rollback (any time)

```bash
sudo systemctl disable --now a4-premarket.timer
sudo -u trader git -C /opt/pipeline checkout v0.7.0
```
