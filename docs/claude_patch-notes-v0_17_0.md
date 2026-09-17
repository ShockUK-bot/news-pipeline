# Patch notes v0.17.0 (2026-09-17, deployed 16:54 CT): A11, the measurement layer

The first of the three unbuilt agents. Deterministic, no model calls, every lane, idempotent, backfills on first run.

## What it does (nightly, `a11-metrics.timer`, 15:20 CT weekdays, after force flat and before A7)

1. `journal.trade_metrics`: one row per closed position in every lane: holding time, MAE and MFE in R from minute bars (daily bars for holds over 80 hours), realised R, exit efficiency (realised R over best R), predicted magnitude (the analyst's estimate carried in the exit policy), realised magnitude, and whether the realisation target was reached.
2. `journal.scanner_counterfactuals` and the scanner rows of `trade_metrics`: the v0.16.0 exit ladder variants, folded in from the standalone job (that timer is now disabled; the script stays for manual use).
3. `journal.counterfactuals` kind `POST_EXIT`: for every final exit, the price path over the rest of the exit day plus the next two sessions, downsampled, with `outcome_r` = R the trade would have made (positive) or avoided losing (negative) had it stayed on. Written once two later sessions exist.
4. `journal.guard_ledger` outcome classification for A12 verdicts whose outcome is known (position closed, or verdict older than 7 days): `SAVE`, `SHAKEOUT` or `NEUTRAL` with `outcome_pnl_r`. For an EXIT or TIGHTEN_STOP verdict, SAVE means the position went on to lose more than 0.25R (exiting would have avoided it), SHAKEOUT means it went on to gain more than 0.25R. For a HOLD verdict, SAVE means holding earned more than 0.25R, SHAKEOUT means it lost more than 0.25R. Price at verdict is the last minute bar before the verdict; the outcome price is the qty weighted exit price after the verdict.
5. `journal.metric_rollups` DAY (last 3 sessions, recomputed each run) and WEEK: trades closed, win rate, sum R, realised P&L, exit efficiency overall and per final exit layer, triage escalation rate, gate pass rate, guard save rate, direction adjusted veto counterfactual, post exit average R, scanner no scale out versus base; breakdowns per lane in `breakdown`, tagged with the config version.

Heartbeat `metrics` (watchdog freshness 4500 minutes, covers the weekend gap).

## Files

`src/a11_metrics/{__init__,metrics,service}.py` (pure functions in `metrics.py`, I/O in `service.py`), `ops/systemd/a11-metrics.service` and `.timer`, `config/watchdog.yaml` (timer and heartbeat entries; `scanner-counterfactual` removed from the timers list), `tests/unit/test_v0_17_0.py`, `CLAUDE.md` services list. No migration: all four tables existed since the phase 4 schema.

## First run (backfill), 16:54 CT

32 trade metric rows added (17 scanner rows were already there from v0.16.0), 37 post exit paths, 157 of 159 guard verdicts classified, 33 DAY and 14 WEEK rollup rows. Unit suite 862 passed. Dry run against live data before the real run.

First readings, week of 09-14: 9 trades closed, win rate 0.78, +6.9R, exit efficiency 0.26 (TRAIL exits 0.57, STOP exits −0.56), gate pass rate 4.8 percent, triage escalation 14 percent. Guard, all time: EXIT verdicts 3 saves, 1 shakeout, 6 neutral; HOLD verdicts 16 saves, 48 shakeouts (the guard kept saying hold on positions that then lost more than 0.25R, FRMI and NVDA most of all), 83 neutral. Post exit: the average trade had −0.01R left on the table over two sessions, so exits are not systematically early; the outliers are the scanner stop outs that kept falling.

## Rollback

Disable `a11-metrics.timer`; re-enable `scanner-counterfactual.timer` if the scanner variants are still wanted nightly. The tables are research only; nothing trades from them.
