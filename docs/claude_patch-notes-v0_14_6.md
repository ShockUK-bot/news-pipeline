# Patch notes v0.14.6

Built 2026-09-13 (Sunday, market closed) from the scanner review and counterfactual review of the same day. Four items, no config changes, no migrations. Previous tag: `v0.14.5`.

## 1. Scanner freshness credit at the day extreme (C10)

**Problem.** `score_candidate` in `src/c10_scanner/rules.py` read `m.minutes_since_extreme or 60`. A candidate exactly at its day high or low (0 minutes) is falsy, so 60 was substituted and the freshest possible candidate scored zero freshness credit instead of the full 0.15. AVAV on 2026-09-10 was journaled at 0.6236 when the formula gives about 0.77.

**Change.** Only `None` is treated as unknown; 0 scores full credit. One line plus a comment.

**Effect.** Scores rise by up to 0.15 for candidates at their extreme when detected. No SCORE_FLOOR reject between 09-04 and 09-11 was at its extreme, so this changes ranking going forward, not any past outcome.

## 2. Pre close evaluation of session invalidations at 15:55 ET (C4)

**Problem.** Session timeframe predicates such as `close_below_prenews` only ran in `session_close_pass` after 16:01 ET. The INVALIDATION exit then could not fill, the catastrophe stop was re placed, and the position was carried to the next day's 14:45 CT overnight exit. All four news positions in the week of 09-08 went that way (BEN, BMY, PLTR, SLG).

**Change.**
- `CompiledPredicate.peek(bar)` in `src/common/invalidation_dsl.py`: evaluates a bar without advancing the persistence streak, marking `fired`, or leaving `_prev` state behind.
- `PositionEngine.preclose_invalidation_pass(session_bar_fn)` in `src/c4_exec/engine.py`: for each open position with a session timeframe exit predicate, builds a provisional session bar and asks `peek`. If it would fire, journals `INVALIDATION_FIRED` with `provisional: true` and executes the INVALIDATION exit at the marketable price while the market is open. Deliberately not `engine.step`: a session bar's low fed to the synthetic stop layer would trip a trail or breakeven stop set after that low printed.
- `engine_loop` in `src/c4_exec/service.py`: runs the pass once per day at or after `PRECLOSE_INVALIDATION_ET = "15:55"` (14:55 CT), before the 15:55 overnight pass so that pass sees the position gone. The session bar is today's minute bars from 09:30 ET with close = last price. Code default; no config knob this release (operator decision 2026-09-13).
- The 16:01 pass is unchanged and remains the confirmation: a no op on a position that is already closed, a normal fire on a position whose pre close exit did not fill.

**Accepted trade off (operator confirmed).** A dip below the pre news price at 14:55 CT that recovers by the close now exits where the close pass would not have.

## 3. Stop slippage journaled on the exit event (C4)

**Problem.** The EXIT and SCALE_OUT position events carried only layer, qty, price and pnl. The level that triggered the exit lived only in the reason string, so slippage had to be reconstructed by joining exits to the last stop event.

**Change.**
- `ExitAction.trigger_price` in `src/c4_exec/exits.py`: the current stop for STOP/BREAKEVEN/TRAIL, the target for TARGET, the bar close for TIME and INVALIDATION.
- `engine._apply`, `force_flat_pass` and `overnight_pass` pass it (the mark for force flat and overnight) to `execute_exit`, which threads it to `record_exit`; the catastrophe path uses the broker stop price.
- `record_exit` in `src/c4_exec/state.py` adds `trigger_price`, `slip_px` and `slip_r` to the event's `new_value` when a trigger is known. Positive is worse than the trigger for either side (sold below a long's stop, covered above a short's); negative means the fill beat it. No migration: `new_value` is jsonb; events without a trigger keep the old shape.

## 4. Replay tool

`ops/tools/scalp_replay.py` replays the `scalp_v1` ladder on the same 1 minute Alpaca bars C4 uses live, for alternative stop multiples and entry times. `--position-id N` reads the trade from the journal; `--ticker`, `--date`, `--entry-time` mode takes the parameters from flags and measures ATR(5m) from the bars before entry if not given. Read only. Usage note in `docs/README.md`. Verified against DELL (position 43) and OKLO (shadow short, 09-11): output matches the Part A tables in `claude_scanner-counterfactual-2026-09-13.md` exactly.

## Files touched

- `src/c10_scanner/rules.py`
- `src/common/invalidation_dsl.py`
- `src/c4_exec/exits.py`, `src/c4_exec/engine.py`, `src/c4_exec/mechanics.py`, `src/c4_exec/state.py`, `src/c4_exec/service.py`
- `ops/tools/scalp_replay.py` (new)
- `tests/unit/test_v0_14_6.py` (new, 18 tests)
- `pyproject.toml` (0.14.5 to 0.14.6), `CLAUDE.md` (version line), `docs/README.md`

## Tests

`env -u PIPELINE_DSN .venv/bin/python -m pytest tests/unit -q`: 785 passed, 3 failed. The three failures are pre existing and unrelated; the same three fail on an untouched checkout of the previous commit in a temporary worktree:
- `test_cik_map.py::test_end_to_end_stored_with_symbols` needs `PIPELINE_DSN` (an integration test living in the unit folder).
- `test_a7_c5.py::test_render_busy_day_with_narrative` and `test_triage_v047.py::test_confidence_required` assert on output shapes from older releases.
These are logged as an open item in the current state file, not fixed here.

## Services to restart

`c10-scanner` (item 1) and `c4-exec` (items 2 and 3). Nothing else. Evening or closed market only; stop `c7-watchdog.timer` before, start it after.

## Verification after restart

- `sudo -n systemctl is-active c10-scanner c4-exec` both `active`.
- `sudo -n journalctl -u c4-exec -n 50 --no-pager` shows `config version active` with the v0.14.6 commit and no tracebacks; same for `c10-scanner`.
- `journal.health` rows `exec` and `scanner` refresh within two minutes.
- Next market day: an EXIT or SCALE_OUT `position_event` carries `trigger_price`, `slip_px`, `slip_r`; at 14:55 CT `journalctl -u c4-exec` shows the pre close pass ran (a `pre-close invalidation` line only if something fired).

## Rollback

On `main`: `git reset --hard v0.14.5` then `sudo -n systemctl restart c10-scanner c4-exec`. No schema to undo. Exit events written under v0.14.6 keep their extra fields, which older code ignores.
