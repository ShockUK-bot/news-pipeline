# v0.12.10 — the confirmation window becomes a window; vetoes become measurable (2026-08-03)

Outcome of the Aug 3 no-trade review (`claude/no-trade-review-2026-08-03.md`):
the first fully-healthy session ran end-to-end and went 0-for-21 at the C3
gate. Two structural problems surfaced, one watchdog nuisance rode along.

## 1. Gate re-check: the 30-minute window was a one-shot check at minute ~4

Every intraday evaluation on Aug 3 ran at `minutes` 3–6 — the v0.11.10
maturity defer schedules the FIRST look ~4 minutes after publish, and a
GATE_NO_CONFIRM verdict was terminal. Effective rule: +1.5% move AND 2.5×
volume within ~4 minutes, or never. Boeing MAX-7 at 09:41 had vol_mult
3.17× (volume confirmed) but pct_move +0.4% at minute 3 — vetoed, never
looked at again.

Now: an intraday veto whose inputs can still change re-defers instead of
journaling — GATE_NO_CONFIRM (price/volume can build), CREDIBILITY (more
outlets can pick up the story), MARKETDATA_MISSING (a bar gap can heal).
Re-checks run every `confirm_recheck_secs` (180) until
`intraday_window_min` expires; the LAST check is scheduled to land before
expiry (`confirm_final_margin_secs`, 45), so the final journaled verdict
carries the real reason and the final numbers — never a misleading
GATE_WINDOW. A thesis now gets ~9 looks across the window instead of one.
Terminal verdicts (LONG_ONLY, GATE_EXTENDED, GATE_WINDOW, PRICED_IN, the
whole open-handoff and scanner branches) are unchanged, and health only
degrades on a FINAL MARKETDATA_MISSING. Uses the existing queue defer
(attempt-refunding, v0.11.10) — no new mechanics.

**Thresholds are NOT loosened.** The placeholders stand until §14 is
designed from the data below.

## 2. journal.gate_counterfactuals: what did each veto cost or save?

Migration 010 (additive only). Every FINAL gate veto — news and scanner
lanes — writes one row with the veto-time state (price, pre-news price,
pct_move, vol_mult, direction, rule, reason). After the session closes, a
sweep inside C3 (`counterfactual_sweep_secs`, 600) pulls the veto→close
minute bars once per row and fills: price 30 min and 2 h after the veto,
the close, and the max favorable/adverse excursion from the veto price.
Best-effort by design: recording failures warn and never block the veto;
unfilled rows retry on later sweeps and are closed out with a note after
48 h. A week of this is the §14 tuning dataset — the review query is in
the module docstring of `src/c3_gate/counterfactual.py`.

## 3. Watchdog: transient systemd states no longer alert

The v0.12.9 deploy restart raced a scheduled pass → false CRITICAL
SERVICE_DOWN ("deactivating") email + RECOVERED minutes later. States
`activating / deactivating / reloading / refreshing` now produce no
finding — recheck next pass. A genuinely dead unit shows inactive/failed
5 minutes later and still alerts; a service wedged mid-activation still
trips HEARTBEAT_STALE. (Backlog item from 2026-08-02, closed.)

## Files

REPLACED: `src/c3_gate/service.py`, `src/c7_watchdog/service.py`,
`config/gate.yaml`, `tests/unit/test_watchdog.py`
NEW: `src/c3_gate/counterfactual.py`,
`schema/migrations/010-gate-counterfactuals.sql`,
`tests/unit/test_gate_recheck.py`, these patch notes, the deploy guide

`src/c3_gate/rules.py` is untouched — the rules stay pure; scheduling
lives in the service.

## Tests

69 green in the release set (test_gate_recheck + test_gate_defer +
test_watchdog + test_analyst_gate + test_schema_vocab), including 12 new
re-check/counterfactual tests and 3 new watchdog transient-state tests.
Full unit suite: 398 green (the 2 known environment-dependent skips
unrelated to this change).

## Config

`config/gate.yaml` gains `confirm_recheck_secs: 180`,
`confirm_final_margin_secs: 45`, `counterfactual_sweep_secs: 600`.
No env changes, no sudoers changes, no new units.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.9
sudo systemctl restart c3-gate
```

Migration 010 is additive; the table can stay in place on rollback.
