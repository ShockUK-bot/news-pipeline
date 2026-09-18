# Patch notes v0.21.0 (2026-09-17, 21:50 CT): gated A12 auto execution; SIC gap fill

## 1. A12 auto execution, gated

Phase 5's deferred item, switched on for the narrow subset A11 showed to be right (the guard's 3 saves were all high urgency EXIT verdicts on a verbatim watch list hit or a correction of the entry story; 0 shakeouts in that subset).

- `src/a12_guard/auto.py` `auto_execute_gate(verdict, item, cfg)`: executes only when `auto_execute.enabled`, the action is in `actions` (`[exit]`), the urgency is in `urgencies` (`[high]`), and, with `require_watch_hit_or_correction: true`, the verdict carries a watch list hit or the item is a correction. Everything else stays advisory. Pure function, tested.
- `src/a12_guard/service.py`: on a gate pass, inside the verdict transaction, the position's `exit_policy` gets a `guard_exit` block (reason, guard decision id, armed time), a `GUARD_ACTION` position event is written, and the ledger row is `auto_executed=true`, `action_taken='EXIT_ARMED'`. The GUARD decision payload carries `auto_execute {execute, why}` on every verdict, executed or not.
- `src/c4_exec/engine.py` `guard_exit_pass()`: every engine pass in session, any open position with `guard_exit` is market exited (layer `GUARD`, 30 bps concession, long sold under the mark, short bought over it). A verdict outside market hours executes on the first pass of the next session. `src/c4_exec/service.py` runs it after the open exit pass.
- `config/a12.yaml` `auto_execute` block. `enabled: false` restores advisory only.
- A11 classifies auto executed verdicts like any other (SAVE / SHAKEOUT / NEUTRAL); A9's guard rule watches `guard_save_rate`. TIGHTEN_STOP and medium urgency exits are deliberately not admitted: no evidence yet.

## 2. SIC gaps

The 44 filers with a CIK but no sector were mostly SIC codes outside the v0.19.0 range table (agriculture 0700, furniture 2510, wholesale 5100s, textiles, rubber, education, misc services), not missing SEC data. `src/common/sectors.py`: those ranges added; a 404 from the submissions endpoint is now retried on a later refresh instead of stored as a permanent unknown; a name heuristic (fund, trust, capital corp, acquisition corp, REIT) marks funds, BDCs and SPACs as Financials with `source='name_heuristic'`; `--refetch-unknown` and `--fill-gaps` commands. After remap: 961 of 1,024 mapped (was 923), 11 by heuristic, 6 filers still unknown, 57 with no US record.

## Tests and services

`tests/unit/test_v0_21_0.py` (gate cases, C4 guard pass long and short, wiring and yaml, new SIC ranges, heuristic and parse source). Suite 883 passed. Restarted `a12-guard` and `c4-exec` after the close.

## Rollback

`config/a12.yaml` `auto_execute.enabled: false` and restart `a12-guard` (C4's pass then never finds an armed position). Or `git reset --hard v0.20.1` on main and restart `a12-guard` and `c4-exec`.
