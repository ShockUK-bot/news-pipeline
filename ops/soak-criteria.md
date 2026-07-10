# Phase 4 Paper-Soak Pass Criteria (v1.0)

Defined BEFORE the soak starts, so the gate decision is read off the journal,
not rationalized afterward. The soak is the Phase 4 exit gate (baseline:
multi-day unattended run). Proposed window: **10 consecutive trading sessions**,
of which the final 5 fully unattended (no operator intervention other than the
daily morning check).

## Hard criteria (any single failure = gate not passed; fix, restart the 5-day
unattended clock)

| # | Criterion | Evidence |
|---|---|---|
| H1 | Every component OK in `journal.health` at each morning check; no stale heartbeats during RTH | soak-check §1, daily log |
| H2 | Zero reconciliation drift on every c4-exec start/restart | c4-exec journal log lines |
| H3 | Every OPEN position has its catastrophe stop broker-side (Alpaca dashboard spot-check daily) AND `catastrophe_stop_order_id` set | soak-check §9 |
| H4 | No code-cleared operator blocks (dead-man ownership rule holds) | `journal.control` audit trail |
| H5 | Backup row fresh (<26h) every day; one restore drill executed on the Spark during the soak | soak-check §2, RUNBOOK §7 |
| H6 | No dead-lettered messages left unexplained | soak-check §3 `dead` column |
| H7 | Every entry is limit-DAY with `client_order_id = intent_id`; no order without a journaled intent | `journal.orders` vs intents |
| H8 | Drawdown breaker: if tripped, trip was correct (−2% verified) and reset was manual | RUNBOOK §5 review notes |

## Soft criteria (targets; misses documented, not blocking)

| # | Criterion | Target |
|---|---|---|
| S1 | Ingestion gaps during RTH | none unexplained; all gap_end non-NULL by close |
| S2 | A1/A2 REJECT rate (strict-JSON discipline under real volume) | < 2% of decisions/day, non-rising trend |
| S3 | `signal.triage` queue latency | oldest pending < 60s during RTH |
| S4 | Dedup quality (hash→bge semantic shift) | daily eyeball of `cluster_corroboration`: no systematic over/under-clustering at the 0.9 threshold |
| S5 | Quarantine | all rows reviewed within 24h |
| S6 | Overnight/session-close passes | D1 matrix + MIP session predicates fire on schedule (15:45/15:55/16:00 ET), zero intraday firings of session-timeframe predicates |

## Data capture

Daily `soak-check.sql` output appended to the soak log (soak-log-template.md).
The log is the §14 gate-threshold tuning input and the Phase 5 handoff evidence.

## Gate decision

PASS = all H1–H8 held for the full window AND soft misses have documented
causes. Output: a dated gate memo in the log; then Phase 5 work may begin.
