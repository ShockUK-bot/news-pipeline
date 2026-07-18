# Deploy Guide — v0.6.0 (Phase 5: A12 Position Guard)

**What this release does:** adds the A12 Position Guard — the agent that
re-reads the news about stocks you HOLD and journals a verdict (hold /
tighten stop / exit) against the original entry thesis. It recommends only;
it cannot trade. It also finally drains the `signal.guard` queue backlog
that has been accumulating since Phase 2.

**When to do this:** any time the market is closed — this weekend is ideal.
Total time: about 20 minutes. No database schema changes, no edits to your
existing configs, no pipeline.env changes, and none of the running services
need a restart (A12 is a brand-new service).

---

## Part 1 — Get the pack onto your PC

1. Download `v0_6_0-pack.zip` from the chat.
2. Right-click it → **Extract All** → extract to a NEW empty folder (e.g. on
   your Desktop). Never re-use an old extraction folder — that's how a stale
   `src` got shipped twice during the A13 deploy. You'll get a folder
   `v0_6_0-pack` containing: `src`, `config`, `ops`, `tests`, and the loose
   files `PATCH_NOTES_v0_6_0.md`, `DEPLOY-v0_6_0.md`.

## Part 2 — Upload to GitHub (browser, same as v0.5.9)

1. Go to `github.com/ShockUK-bot/news-pipeline`.
2. Click **Add file → Upload files**.
3. Drag in, from the extracted folder: the **src**, **config**, **ops**, and
   **tests** folders, plus the two loose `.md` files. (Folder paths are
   preserved. Every file in this pack is NEW — nothing overwrites.)
4. Commit message: `v0.6.0: Phase 5 — A12 position guard (verdict-only)`
5. Click **Commit changes**.
6. **Verify the commit** (the A13 lesson): open the commit you just made
   (repo page → the clock/history icon → top entry) and confirm it shows
   **13 files added**, including `src/a12_guard/service.py` with real green
   lines. If any expected file is missing, stop and tell Claude.

## Part 3 — Bump the version number (pencil edit)

1. On the repo page, open `pyproject.toml` → click the **pencil** icon.
2. Find the line `version = "0.5.9"` and change it to `version = "0.6.0"`.
3. **Commit changes** (make sure it commits to `main`, not a new branch).
4. Now cut the release: **Releases → Draft a new release** → tag `v0.6.0`
   on `main` → title `v0.6.0 — A12 Position Guard` → **Publish**.

## Part 4 — Pull v0.6.0 onto the Spark

Open a terminal on the Spark (the usual way):

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.6.0
```

You should see it mention switching to `v0.6.0`. Any error about "local
changes would be overwritten" → STOP and paste it to Claude.

## Part 5 — Run the tests on the Spark

```bash
cd /opt/pipeline
sudo -u trader bash -c 'export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test MARKETDATA=fake BROKER=fake \
  PIPELINE_DSN=postgresql://trader:PASSWORD@127.0.0.1:5432/trading_test && .venv/bin/python -m pytest tests/ -q'
```

(Replace `PASSWORD` with the trader database password — same one as in
`/etc/pipeline/pipeline.env`.) The last line must say **0 failed** and the
total should be 23 higher than v0.5.9's count. Any FAILED → stop, paste the
output to Claude, nothing has been started yet.

## Part 6 — Check the analyst model is up, then probe the grammar

A12 talks to the same analyst server as A2. On a weekend it may be stopped
(especially if the heavy model is running). Check:

```bash
curl -s http://127.0.0.1:8081/health
```

If that does NOT print `{"status":"ok"}`, start it and give it ~2 minutes:

```bash
sudo systemctl start llama-a2
```

Then the mandatory schema probe (same gate as every A13 deploy):

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python ops/a12-schema-probe.py'
```

Expect exactly: `guard_verdict  HTTP 200`. A 400 means the grammar didn't
compile on your llama.cpp build — do NOT continue; tell Claude.

## Part 7 — Install and start the A12 service

```bash
sudo cp /opt/pipeline/ops/systemd/a12-guard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now a12-guard
```

Watch it come up and drain the old backlog:

```bash
sleep 5 && sudo journalctl -u a12-guard -n 40 --no-pager
```

You should see an `A12 up` line, then a burst of `guard signal expired` /
`guard signal without open position` lines — that's the ~44 orphaned
messages from before Phase 5 being drained and journaled (they're old news;
nothing evaluates against the model). Then confirm:

```bash
systemctl is-active a12-guard
```

→ `active`.

## Part 8 — Verify

The backlog is gone:

```bash
sudo -u trader psql -d trading -c "SELECT count(*) FROM queue.messages WHERE queue_name='signal.guard' AND done_ts IS NULL;"
```

→ `0` (or close — it drains within a minute or two).

The drain was journaled, and the guard heartbeat is alive:

```bash
sudo -u trader psql -d trading -c "SELECT action, count(*) FROM journal.decisions WHERE stage='GUARD' GROUP BY 1;"
sudo -u trader psql -d trading -c "SELECT component, status, detail FROM journal.health WHERE component='guard';"
```

Expect mostly `EXPIRED` (plus maybe `NO_POSITION`), and `guard | OK`.

If you started `llama-a2` just for the probe and the heavy model needs the
memory back tonight, you can stop it again (`sudo systemctl stop llama-a2`)
— A12 will wake it by itself if a guard signal arrives (and journals
ALERT_ONLY if it can't).

## Part 9 — What changes from Monday

Nothing about entries. The first time the system actually holds a position
and news touches that ticker, you'll see a `GUARD` row appear on the
dashboard decision tape within about a minute — thesis intact or not, the
recommended action, and which invalidation from the original thesis (if
any) the news matched. The running record lives in:

```sql
SELECT ts, position_id, thesis_intact, recommended_action, urgency
FROM journal.guard_ledger ORDER BY ts DESC LIMIT 20;
```

A12 only recommends. Acting on an EXIT verdict is your call, from the
broker app or the dashboard kill switch if it's urgent — auto-execution
stays off until we promote it deliberately, with journal evidence, through
the Sunday channel.

## Rollback (any time, ~2 minutes)

```bash
sudo systemctl disable --now a12-guard
sudo -u trader git -C /opt/pipeline checkout v0.5.9
```

Everything else keeps running exactly as before; the guard queue just goes
back to accumulating (harmless — chat ignores stale guard messages).
