# Deploy Guide — v0.7.0 (Phase 6: A7 EOD Report + C5 Mailer)

**What this release does:** every trading day at 3:35 PM Chicago time, the
system emails you a complete end-of-day report — trades, P&L, open positions,
guard verdicts, vetoes, system health. All numbers computed by code; a short
model-written summary on top (the first real job for your staged 122B heavy
model). A separate "mailer" service is the only thing that ever touches your
email password.

**When to do this:** any time — nothing here touches trading services. Total
time: about 30 minutes including the Gmail app-password setup. The smoke
test at the end sends you a real email today.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_7_0-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `v0_7_0-pack` containing: `src`, `config`, `ops`, `schema`, `tests`, and
   the loose files `PATCH_NOTES_v0_7_0.md`, `DEPLOY-v0_7_0.md`.

## Part 2 — Upload to GitHub (browser, same as v0.6.0)

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **src**, **config**, **ops**, **schema**, **tests** folders
   and the two loose `.md` files. Every file is NEW — nothing overwrites.
3. Commit message: `v0.7.0: Phase 6 — A7 EOD report + C5 mailer`
4. **Commit changes**, then open the commit and confirm **17 files added**,
   including `src/a7_report/service.py` and
   `ops/systemd/a7-eod.timer` with green lines. Anything missing → stop,
   tell Claude.

## Part 3 — Version bump + release

1. Open `pyproject.toml` → pencil icon → change `version = "0.6.0"` to
   `version = "0.7.0"` → **Commit changes** (to `main`).
2. **Releases → Draft a new release** → tag `v0.7.0` on `main` → title
   `v0.7.0 — A7 EOD report + C5 mailer` → **Publish**.

## Part 4 — Pull onto the Spark and apply the migration

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.7.0
```

Then the (additive, safe) outbox migration — on BOTH databases:

```bash
sudo -u trader psql -d trading      -f /opt/pipeline/schema/migrations/003-outbox.sql
sudo -u trader psql -d trading_test -f /opt/pipeline/schema/migrations/003-outbox.sql
```

Each should print `ALTER TABLE`.

## Part 5 — Run the tests on the Spark

```bash
cd /opt/pipeline
sudo -u trader bash -c 'export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test MARKETDATA=fake BROKER=fake \
  PIPELINE_DSN=postgresql://trader:PASSWORD@127.0.0.1:5432/trading_test && .venv/bin/python -m pytest tests/ -q'
```

(Same PASSWORD substitution as last time. If you see qdrant `.lock`
permission errors again: `sudo rm -rf /tmp/qdrant-*` and re-run.) Expect
**305 passed, 0 failed** — 19 more than the v0.6.0 deploy.

## Part 6 — Create the email credentials (Gmail app password)

The mailer needs its own password — NOT your normal Gmail password:

1. On your PC, go to `myaccount.google.com` → **Security**. Make sure
   **2-Step Verification** is ON (the mailer setup requires it).
2. In the search box at the top of that page type **App passwords** and open
   it. App name: `Trading Pipeline` → **Create**.
3. Google shows a 16-character password (four groups of four). Copy it —
   you'll paste it once below, then you can forget it.

Now on the Spark, create the mailer's private env file:

```bash
sudo nano /etc/pipeline/mailer.env
```

Type these six lines, with your address and that 16-character password
(remove the spaces Google shows in it):

```
MAILER_SMTP_HOST=smtp.gmail.com
MAILER_SMTP_PORT=465
MAILER_SMTP_USER=ian.gillbanks@gmail.com
MAILER_SMTP_PASS=abcdabcdabcdabcd
MAILER_FROM=Trading Pipeline <ian.gillbanks@gmail.com>
MAILER_TO=ian.gillbanks@gmail.com
```

Save (Ctrl+O, Enter, Ctrl+X), then lock it down so only root/systemd can
read it — no agent process can:

```bash
sudo chmod 600 /etc/pipeline/mailer.env
sudo chown root:root /etc/pipeline/mailer.env
```

## Part 7 — Install the sudoers rule and the services

The heavy model start/stop permission (one command pair, nothing else):

```bash
sudo cp /opt/pipeline/ops/sudoers.d/a7-heavy /etc/sudoers.d/a7-heavy
sudo chmod 440 /etc/sudoers.d/a7-heavy
sudo visudo -c
```

The last command must end with `parsed OK` (it validates sudo's config —
important). Then the services and timers:

```bash
sudo cp /opt/pipeline/ops/systemd/a7-eod.service /opt/pipeline/ops/systemd/a7-eod.timer /etc/systemd/system/
sudo cp /opt/pipeline/ops/systemd/c5-mailer.service /opt/pipeline/ops/systemd/c5-mailer.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now a7-eod.timer c5-mailer.timer
systemctl list-timers a7-eod.timer c5-mailer.timer --no-pager
```

You should see both timers with a NEXT time (a7-eod next trading weekday
16:35 ET; c5-mailer within 5 minutes).

## Part 8 — Smoke test: send yourself today's report NOW

Weekends are non-session days (the report normally skips them), so this
one-off command overrides that for the test. It will also start the heavy
model (takes a few minutes to load ~77 GB), narrate, and stop it again:

```bash
sudo -u trader env PYTHONPATH=/opt/pipeline/src bash -c 'set -a; source /etc/pipeline/pipeline.env; set +a; cd /opt/pipeline && .venv/bin/python - <<PY
import asyncio
from common.config import config_path, load_yaml
from common.journal import register_config_version
from a7_report.service import run_report

async def main():
    cfg = load_yaml(config_path("a7.yaml"))
    cfg.setdefault("report", {})["send_on_nonsession"] = True
    await register_config_version("a7 smoke test")
    outbox_id = await run_report(cfg)
    print("outbox row:", outbox_id)

asyncio.run(main())
PY'
```

Be patient — "outbox row: 1" (or similar number) can take 5–10 minutes the
first time (heavy model load + a slow, careful narration). Then trigger the
mailer instead of waiting for its timer:

```bash
sudo systemctl start c5-mailer.service
sudo journalctl -u c5-mailer -n 10 --no-pager
```

Look for a `sent` line — and **check your inbox** (and spam folder, the
first one sometimes lands there). Also verify the heavy model was stopped
again:

```bash
systemctl is-active llama-heavy
```

→ `inactive` (unless you had started it yourself earlier — then A7 left it
alone, by design).

**If the email didn't arrive:** `sudo journalctl -u c5-mailer -n 30
--no-pager` — an authentication error means the app password was mistyped
(redo Part 6's nano step); then `sudo systemctl start c5-mailer.service`
again — the report is still queued, nothing is lost.

**If the narrative section says "narrative unavailable":** the report and
email still work end-to-end; it means the heavy model didn't come up in
time and the analyst wasn't reachable either. Tell Claude, but nothing is
broken.

## Part 9 — What changes from Monday

At 3:35 PM Chicago every trading day, the report lands in your inbox
automatically. First scheduled one: **Monday**. It's also the first day of
SIP data, so that email is where you'll see the day's veto mix and (maybe)
the first real trade — bring its numbers to the weekend session for the
gate-threshold tuning.

## Rollback (any time, ~2 minutes)

```bash
sudo systemctl disable --now a7-eod.timer c5-mailer.timer
sudo -u trader git -C /opt/pipeline checkout v0.6.0
```

The migration is additive — leave it. Delete `/etc/pipeline/mailer.env` only
if you want the credentials gone too.
