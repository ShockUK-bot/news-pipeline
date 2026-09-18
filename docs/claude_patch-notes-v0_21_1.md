# Patch notes v0.21.1 (2026-09-17, 21:55 CT): after close pass no longer fires stops after hours

## Incident

At 21:38 CT, right after the v0.21.0 restart of `c4-exec`, the engine tried to sell 34 RIOT after hours; Alpaca rejected it (`insufficient qty available`), the position's catastrophe stop was left `pending_cancel`, and the next two passes hit a 422 cancelling it again. Cause: a fresh process re-runs the 16:01 ET session close pass, which fed the full session bar (high 22.065, low 20.65) to `engine.step`; C11 had tightened RIOT's stop to 21.77 at 21:15, above the day's low, so the L1 stop fired on a bar that predates the stop. No order was placed. The 10.67 catastrophe stop it disturbed sat 50 percent below the price, so no protection was lost in practice; RIOT's synthetic 21.77 stop and the 09:35 ET sale cover tomorrow.

## Fix

- `src/c4_exec/engine.py` `session_close_pass`: the session bar's high and low are collapsed to the close, so only a close through a stop fires (what a stop would do at the next open anyway); each position runs in its own try so one broker error is logged, not raised.
- `src/c4_exec/service.py`: the session close pass is not re-run by a process started after 17:30 ET (`SESSION_CLOSE_PASS_UNTIL_ET`); the 16:01 process already ran it.
- `tests/unit/test_v0_21_1.py`. Suite 885 passed. `c4-exec` restarted after the fix; no error on the restart.
- Gotcha recorded in `CLAUDE.md`.

## Rollback

`git reset --hard v0.21.0` on main, restart `c4-exec` (and do not restart it in the evening after a C11 run).
