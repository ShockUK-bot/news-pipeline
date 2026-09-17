# Patch notes v0.16.0 (2026-09-17, deployed 16:27 to 16:35 CT, after the close)

Follows `claude_scanner-review-2026-09-17.md` (17 trades, +1,378, 11 winners). Operator approved all recommendations on 2026-09-17.

## 1. Scanner lane sizing (live trading change)

- `config/risk.yaml` `scanner.risk_multiplier` 0.5 to **1.0** (full 0.5 percent risk per trade, about 500 dollars).
- `config/risk.yaml` `scanner.max_position_notional_pct: 0.30` (new): a lane specific notional cap for scanner entries, replacing the global 15 percent for this lane only. The global cap is unchanged for the news and thesis lanes.
- `src/a3_risk/sizing.py` `scanner_capital_cfg()` builds the lane policy; `src/a3_risk/service.py` uses it on the scanner branch.

Why 30 and not the 25 in the review: A3's viability rule vetoes a trade whose clipped risk is under half the budget (`SIZE_CLIPPED`). With a 1.0x budget that threshold is a stop of at least 1.0 percent of price under a 25 percent cap, which would have vetoed 5 of the 17 trades to date (KLAC, both SKHY, SPCX 09-10, CRM at 0.91 to 0.92 percent). Under a 30 percent cap the threshold is 0.83 percent, identical to today's, so the admitted set is unchanged and every trade is about twice the size.

Effect: a large cap scalp with a 1 percent stop now sizes to about 29,000 dollars notional with about 290 at risk (was 14,600 and 145). Wider stops size on risk up to 500. Worst day at the 5 trade cap is about 2,500 dollars, 2.5 percent of capital. Concurrency stays at 2, so up to about 60,000 deployed in scanner names at once; longs are also clipped by settled cash.

## 2. Nightly scanner counterfactuals (research)

- `schema/migrations/017-scanner-counterfactuals.sql`: `journal.scanner_counterfactuals` (position, variant, pnl, R, MFE, close R, exits). Applied 16:27 CT, `dash_reader` granted.
- `ops/tools/scalp_replay.py`: `replay()` takes variant options (trail multiple, time stop off, no scale out, runner hold), returns MFE and the 14:50 mark; `VARIANTS` and `replay_variants()`; `load_position()` derives ATR(5m) from the journaled R unit so promoted positions replay correctly. CLI unchanged.
- `ops/scanner_counterfactuals.py`: for every closed scanner position without rows, replays all seven variants on Alpaca minute bars and journals them, and fills `journal.trade_metrics` (holding time, MAE, MFE, realised R, exit efficiency, predicted vs realised magnitude, target hit). Prints a totals table with `--report`. Heartbeat `scanner_cf`.
- `ops/systemd/scanner-counterfactual.service` and `.timer` (15:05 CT weekdays, after force flat, before A7). Installed and enabled. `config/watchdog.yaml` timers list updated.

First run backfilled all 17 trades (119 rows). Totals against the current ladder: no scale out +425, 3.0 ATR stop +14, no time stop −52, runner hold −124, trail 2.5 −240, trail 3.0 −358. Same picture as the review; the decision point is 30 trades.

## 3. Tests

`tests/unit/test_v0_16_0.py`: lane policy helper, sizing under both policies including the 25 percent viability veto, yaml pins, replay variants on synthetic bars, CLI compatibility, unit and watchdog wiring. Suite 850 passed.

## Services

`a3-risk` restarted (reads risk.yaml at startup). `scanner-counterfactual.timer` enabled, next run 2026-09-18 15:05 CT. Nothing else touched.

## Rollback

`config/risk.yaml`: `risk_multiplier: 0.5`, delete the `max_position_notional_pct` line under `scanner:`, then `sudo -n systemctl restart a3-risk`. Or `git reset --hard v0.15.4` on main and restart `a3-risk`. The counterfactual job and table are research only and can stay.
