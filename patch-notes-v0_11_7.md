# v0.11.7 — Health/observability polish (heartbeats, dead-man map, EDGAR resilience)

Four small, low-risk fixes that clear two persistent DEGRADED health lines and
close a real monitoring gap. **No trading logic, gates, sizing, or exec
changes.**

## 1. C3 gate periodic heartbeat (`src/c3_gate/service.py`)

C3 wrote its `journal.health` row only at startup, so 5 minutes after any
restart the dead-man saw a stale timestamp and flagged `stale: gate` forever
(this is the "Deadman stale: gate 1522min" you saw). Added a 60-second
heartbeat in the consume loop, mirroring A2. The gate line now stays fresh.

Important: this was only ever an **ALERT**, never a block — `gate` in
`config/deadman.yaml` has `alert_min` only, no `block_entries_min`, so it could
not have blocked trades. The real problem was alert-masking: the dead-man is the
early-warning for a genuinely dead ingestion/marketdata (which *do* block /
suspend), and it was stuck yellow on a false gate alarm.

## 2. A1 triage periodic heartbeat (`src/a1_triage/service.py`)

Same fix for A1 (also startup-only). Needed so the dead-man can tell a live A1
from a dead one once #3 lands.

## 3. Dead-man component-map fix (`src/c4_exec/deadman.py`)

Latent bug: the monitor's `COMPONENT_MAP` looked up health rows named
`triage_model` / `analyst_model`, but A1/A2 write `triage` / `analyst`. Those
names matched no row, so the dead-man **silently skipped triage and analyst —
never monitoring them at all.** Fixed the map to the real names. Combined with
#1/#2, the dead-man now genuinely watches ingestion, marketdata, gate, triage,
and analyst. (triage/analyst have `alert_min` only — alert, never block — so
this adds monitoring with zero new blocking behaviour.)

## 4. EDGAR timeout + failure tolerance (`src/c1_ingestion/sources/edgar.py`)

The large `all-filings` feed sometimes exceeded the 20s HTTP timeout when SEC
was slow, and a single `ReadTimeout` immediately flipped health to DEGRADED
(this is the "edgar all-filings: ReadTimeout" line). Two changes, both
config-tunable:

- HTTP timeout `20s → 45s` (`http_timeout_secs`, default 45).
- Health goes DEGRADED only after **3 consecutive** failures of the same feed
  (`degrade_after_failures`, default 3), resetting to OK on any success — so a
  transient slow poll no longer paints the dashboard yellow. A real, sustained
  outage still surfaces.

## Validation

Compiles; dashboard/gate/triage/deadman suites (`test_risk_exec_flow`,
`test_analyst_gate_flow`, `test_triage_router`) **42 passed**; EDGAR suites
(`test_edgar_storm_regression`, `test_cik_map`, `test_normalize`) **48 passed**.
EdgarSource confirmed to expose the new tunables (45s / 3 by default).

## Deploy note (restart ordering)

`c3-gate`, `a1-triage`, `c1-ingestion` can restart any time (stateless
consumers) and immediately clear the visible DEGRADED lines. The `c4-exec`
restart (which activates #3) is best done **at market close** — it's the
execution engine and reconciles broker state on boot, so a closed-market
restart is cleaner. The map fix is not urgent; the gate line already clears from
#1 alone.

## Rollback

`git checkout v0.11.6d` and restart the four services. No DB/schema changes.
