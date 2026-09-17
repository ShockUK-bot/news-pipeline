# Patch notes v0.16.1 (2026-09-17, deployed after the close)

Closes the open items from the 09-15 to 09-17 reviews that were code, not measurement.

## 1. Dead and review-exit theses are sold at the next open (RIOT)

- `src/c4_exec/engine.py` `open_exit_pass()`: from 09:35 ET every engine pass market-exits any open position whose `exit_policy.exit_at_open` block is set (layer `REVIEW`, reason in the exit event). Pure code, idempotent.
- `src/c4_exec/service.py`: the pass runs right after the promotion pass, before force flat; `OPEN_EXIT_ET = "09:35"` code default.
- `src/c11_thesis/service.py`: when the management pass decides DEAD or REVIEW it still tightens the stop (belt and braces for a dip before 09:35) and now also arms `exit_at_open` (`arm_exit_at_open()`); a position already armed is skipped. `config/thesis_entry.yaml` `management.dead_exit_at_open: true` (false restores the stop only behaviour).
- RIOT: tonight's 21:15 CT C11 run arms it; C4 sells it at 09:35 ET on 2026-09-18 whatever the gap.

## 2. Re-entry cooling off (INVX)

- `src/c11_thesis/service.py` `recent_forced_exits(days)`: tickers with a STOP, CATASTROPHE, INVALIDATION, REVIEW, GUARD, BREAKER or BREAKEVEN exit in any lane inside the window are skipped at entry planning with `THESIS_SKIP` reason `COOLOFF`. `config/thesis_entry.yaml` `entry.reentry_cooloff_days: 5` (0 = off).

## 3. Dashboard cosmetics

- Gate Lab: the extended hours shadow scoreboard and would-trade table are direction adjusted (a down shadow trade is a short; a falling close is its gain). Previously raw price change.
- Gate Lab: `rule='fade'` rows no longer appear under the regular hours veto table; they have their own "Fade shadow lane" panel with numbers in the fade's direction (empty until the first fade candidate).
- BURST tab: scoreboard gains four columns from the v0.15.3/v0.15.4 data: realistic n, realistic 30 minute return, 0.5/0.5 target and stop rates. Description text updated.

## 4. Closed without code

- Scanner freshness credit (`or 60`): already fixed in v0.14.6, confirmed in `src/c10_scanner/rules.py`.

## Not done, needs the operator

- Model file tidy: see the session summary for the exact list; deletion needs explicit approval.
- Qwen3.8-Flash-Next evaluation: the model is not on disk and the box runs offline; needs a download first.
- Scanner sector cluster rule: needs a sector data source (every trade still carries `SECTOR_UNKNOWN`).

## Tests and services

`tests/unit/test_v0_16_1.py` (arm block, tighten still tighten only, open exit pass timing and pricing for long and short, service wiring, yaml pins, dashboard shapes). Suite 856 passed. Dashboard queries exercised against live Postgres before restart.

Restarted: `c4-exec`, `c6-dashboard`. `thesis-entry` is a timer and picks the change up at 21:15 CT.

## Rollback

`git reset --hard v0.16.0` on main, restart `c4-exec` and `c6-dashboard`. Or leave the code and set `dead_exit_at_open: false` and `reentry_cooloff_days: 0` in `config/thesis_entry.yaml` (no restart needed, C11 is a oneshot).
