# v0.11.6 — Queue-table prune job + C6 "Pipeline load" panel

Two additive operations/observability features from the 2026-07-20 incident
review. **No agent, model, or trading-logic changes** — nothing that can affect
what the system trades. Safe to deploy any time.

---

## Feature 1 — Automatic prune of the queue table

**Why.** `queue.messages` is transport, not the audit log: `ack`/DLQ sets
`done_ts` but never deletes the row, so the table grows without bound (this is
what made a plain `count(*)` look like a 5,800-message "backlog" on 2026-07-20
when the real pending count was zero). The permanent record of every decision
lives in `journal.decisions`, so deleting old *completed* queue rows loses
nothing.

**What.** A small nightly job deletes `queue.messages` rows whose `done_ts` is
older than `QUEUE_PRUNE_KEEP_DAYS` (default **7**). It only ever touches rows
with `done_ts` set — pending and in-flight work is never removed — and it
journals its result to `journal.health` under component `queue_prune`, so the
dashboard shows the last prune.

New files:

- `ops/prune-queue.sh` — the prune (a `DELETE … WHERE done_ts < now() - Nd`,
  guarded so a non-integer retention value can't be injected into the SQL).
- `ops/systemd/queue-prune.service` — oneshot, runs as `trader`, reads
  `PIPELINE_DSN` from `/etc/pipeline/pipeline.env`.
- `ops/systemd/queue-prune.timer` — fires daily at 03:10 ET.

Retention is tunable without a code change: set `QUEUE_PRUNE_KEEP_DAYS=<n>` in
`/etc/pipeline/pipeline.env`.

## Feature 2 — "Pipeline load" panel on the C6 dashboard (LIVE tab)

**Why.** Today's analyst overload was invisible until we queried the DB by
hand. This surfaces it on the console so it's obvious at a glance.

**What.** A new panel showing, per queue, the *real* pending depth mapped to
the agent that consumes it:

- **Ready** — messages waiting to be picked up (green / amber ≥50 / red ≥200).
  `signal.analyst` climbing = A2 overloaded; `signal.triage` = A1; etc.
- **In-flight** — currently being processed.
- **Oldest wait** — age of the oldest waiting message (amber ≥60s, red ≥300s).
- **Repeat-analysis watch** — any ticker analyzed >3× in 30 min, shown as a
  chip (amber, or red ≥10). This is the direct fingerprint of a loop like the
  sympathy-lane cascade; YUM would have lit up red today.

The depth query only scans not-done rows (index-backed), so it's cheap on the
1.5s live push. It reads `queue.messages` and `journal.decisions` directly — no
schema change, no new DB view.

Changed files:

- `dashboard/app.py` — `/api/state` now returns a `load` block (per-queue
  ready/in-flight/oldest-age + hot tickers). (REPLACED)
- `dashboard/index.html` — renders the "Pipeline load" panel. (REPLACED)
- `tests/integration/test_dashboard.py` — `test_state_load_panel` asserts the
  new shape and that a ready / claimed message is counted correctly. (REPLACED)

## Validation

Verified against a real PostgreSQL 16: dashboard suite **9/9** (incl. the new
load-panel test); the prune script confirmed to delete only old completed rows
(pending + recent kept), write its health row, and reject a non-integer
retention value; the dashboard JavaScript passes `node --check`; and the
v0.11.5 analyst-gate suite still **9/9** (this release doesn't touch it).

## What this does NOT touch

No agents, no models, no gates, no risk/exec, no trading logic, no config
schema. Feature 1 is a housekeeping timer; Feature 2 is a read-only panel.

## Rollback

`git checkout v0.11.5` on the Spark and `sudo systemctl restart c6-dashboard`
(reverts the panel). To stop the prune job:
`sudo systemctl disable --now queue-prune.timer`.
