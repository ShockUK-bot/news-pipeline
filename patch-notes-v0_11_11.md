# v0.11.11 — Health-row recovery for regime + RSS aggregate (2026-07-22)

Found from the C6 dashboard showing two red lines while the system was
actually healthy:

- `regime  DEGRADED  ConnectError('All connection attempts failed')`
- `ingestion:rss  DEGRADED  all feeds failing`

Code-only fix, two files changed, no schema changes, no new env vars, no new
timers, no new sudoers. Builds on v0.11.10.

## Symptom

Both dashboard rows were **stale latches**, not live outages. The
`journal.health` table proved it:

| component        | status   | detail                                           | updated_ts (frozen)     |
|------------------|----------|--------------------------------------------------|-------------------------|
| `ingestion:rss`  | DEGRADED | all feeds failing                                | 2026-07-21 18:31:50 CDT |
| `regime`         | DEGRADED | ConnectError('All connection attempts failed')   | 2026-07-21 14:37:02 CDT |

Meanwhile the three per-feed rows (`ingestion:rss:prnewswire-news`,
`…:globenewswire-public`, `…:businesswire-all`) were all **OK** and updating
every 60 seconds, EDGAR/Alpaca were fine, and `c8-regime` was writing a healthy
snapshot every interval. Two brief, unrelated network hiccups the previous day
(14:37 and 18:31) had tripped these rows to DEGRADED; both recovered within
seconds, but the rows never went back to green.

## Root cause

The same bug shape in two independent services: each writes its health row to
OK **once at startup** and to DEGRADED **on failure**, but never re-asserts OK
after recovering. A single transient error therefore latches the dashboard red
until the service is restarted.

1. **`src/c8_regime/service.py`** — `main()` set `regime` = OK before the loop,
   then only ever set DEGRADED inside the `except`. A successful snapshot did
   not touch health, so the row stayed DEGRADED forever after the first blip.

2. **`src/c1_ingestion/sources/rss.py`** — `run()` set the aggregate
   `ingestion:rss` = OK once at startup and = DEGRADED "all feeds failing" when
   every feed failed in one pass, but never wrote OK again when feeds recovered.
   (The per-feed `ingestion:rss:<name>` rows added in v0.11.1 already reset
   correctly — that's why they were green while the aggregate was stuck red.)

This is the exact failure family v0.11.7 fixed for EDGAR (reset to OK on
success; degrade only after N consecutive failures). It just hadn't been
applied to these two.

## Fix

1. **Regime (`c8_regime/service.py`).** Every successful snapshot now
   re-asserts `regime` = OK (`"snapshot <id>"`), keeping the row fresh. Failures
   increment a consecutive-failure counter and only flip the row to DEGRADED
   after `degrade_after_failures` in a row (new optional `config/c8.yaml` key,
   **default 2** — no config change required to get the default). A single
   transient `ConnectError` no longer shows on the dashboard at all; a genuine
   sustained outage still surfaces after two consecutive misses. Extracted the
   decision into a pure `regime_health()` helper for testing.

2. **RSS aggregate (`c1_ingestion/sources/rss.py`).** The aggregate
   `ingestion:rss` row is now recomputed and rewritten **every poll cycle**, so
   a recovery clears it automatically (`"OK — N/M feeds OK"` or
   `"OK — M feeds, every Ns"`). It flips to DEGRADED only after
   `aggregate_degrade_after` consecutive all-feeds-down passes (new optional
   `sources.yaml: rss.aggregate_degrade_after`, **default 2**), so a one-off
   simultaneous blip — exactly what happened at 18:31 — no longer trips it.
   Extracted the decision into a pure `aggregate_health()` helper for testing.
   Per-feed rows, the `GapMonitor`, and all dead-man logic are untouched.

**No trading logic, gates, sizing, exit, or execution behaviour is changed.**
These are health/observability rows only. Neither `regime` nor `ingestion:rss`
is a dead-man-monitored component, so nothing about blocking or suspension is
affected.

## Changed files

- `src/c8_regime/service.py` (full replacement)
- `src/c1_ingestion/sources/rss.py` (full replacement)
- `tests/unit/test_health_recovery.py` (new — 11 tests)

## Tests

New `tests/unit/test_health_recovery.py` (11 tests) pins both helpers:
reset-to-OK on recovery, transient-tolerance (one failure = no red light), and
degrade-after-threshold, for both `regime_health()` and `aggregate_health()`.
Pure functions — no DB or network. Both source files compile; the 16-assertion
logic replay (including yesterday's incident) passes.

## Deploy

Upload pack → tag v0.11.11 → pull → restart `c8-regime` and `c1-ingestion`.
Safe any time (both are stateless health-reporters); the `c1-ingestion` restart
causes a ~2-second news gap, so market-closed is the tidiest window if you'd
rather wait. See `v0_11_11-deploy-guide.md`.

## Rollback

`git checkout v0.11.10` then restart the two services. No DB/schema/timer
changes to undo.
