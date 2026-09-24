# Patch notes v0.26.2 (2026-09-24, 09:09 CT, deployed during the session on the operator's "do it now"): promotion keeps the trail; swing profile profit lock

## Why

HOOD (scanner long 09-18, runner promoted to the swing profile at 11:46 that day) peaked at 1,318 dollars of runner profit on 09-21 and had 561 left on the morning of 09-24. The promotion had swapped its 1.5 x 5 minute ATR trail (2.40 dollars behind the high) for the swing profile's 2.5 x daily ATR trail (17.30 dollars behind), which sat below the 118.59 the scalp trail had already reached; ratchets only tighten, so the stop never moved again in four sessions. The v0.26.0 profit lock was on the thesis profiles only.

## Changes

- `src/c4_exec/engine.py` `promoted_policy`: with `promotion_keeps_trail: true` (default) a promoted scalp keeps its trail, breakeven and ATR basis; only the overnight rule and the time stop graduate. `false` restores the old swap.
- `effective_policy` (pure, applied in memory each pass, no journal edits): the live profile's `profit_lock` for policies that predate it, and for positions promoted before this release (daily ATR trail journaled) the entry trail with the 5 minute ATR recovered as `r_unit / initial k`.
- `config/exit_profiles.yaml` `short_term_v1`: `profit_lock: {activate_pct: 0.05, trail_pct: 0.04}` and `promotion_keeps_trail: true`.
- Tests: `tests/unit/test_v0_26_2.py`; `test_promotion.py` and `test_v0_26_0.py` pins updated to the new design.

## Effect on HOOD

First engine pass after the restart (09:09 CT): trail ratcheted to 125.56 (1.5 x 0.785 behind the 126.74 high), price 121.3 was below it, TRAIL exit filled at 121.32 for +628.30 on the 127 share runner (the target half had banked +120.68 on 09-18). Total HOOD +748.98.

## Services

`c4-exec` restarted at 09:09 CT with the operator's explicit "do it now, market open". Suite green at time of commit.

## Rollback

`promotion_keeps_trail: false` and remove the `short_term_v1` `profit_lock` line, restart `c4-exec`; or `git reset --hard v0.26.1` on main and restart `c4-exec`.
