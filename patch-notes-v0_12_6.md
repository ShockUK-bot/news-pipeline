# v0.12.6 — Scale-out at target fires once, not every minute (2026-07-24)

**Fixes the runaway profit-taking discovered ~4 minutes after v0.12.5 went
live: the realization layer (sell half at target, let the rest ride)
re-fired on every bar while price sat at target, halving the position each
minute.** One code change in one file, one new regression test. No schema
change, no config change, no systemd change. One service restart
(`c4-exec`). **Deploy before any open position reaches its profit target.**

## What actually happened (JNJ, position 5)

v0.12.5 revived the exit engine (it had been crashing on every live bar),
and at 10:31:18 the realization layer evaluated real market data for the
first time ever. JNJ was above its materialized target, so L4 correctly
fired `scale_out_50` — sell half (9 of 19 shares), keep 10 running. But
then it fired again the next minute. And the next:

```
10:31:18  TARGET  qty 9   @ 262.7600   (19 -> 10)
10:32:20  TARGET  qty 5   @ 262.8000   (10 -> 5)
10:33:23  TARGET  qty 2   @ 262.9150   ( 5 -> 3)
10:34:25  TARGET  qty 1   @ 262.9600   ( 3 -> 2)
10:35:28  TARGET  qty 1   @ 262.7500   ( 2 -> 1)
```

It stopped only because `half = 1 // 2 = 0` sells nothing. Every fill was
profitable (~+0.5R per share, ≈ $113 realized total) — no money was lost —
but the position was dismantled instead of letting 10 shares ride, which
is the entire point of the scale-out design.

## The bug

`exits.py` gates L4 on `state["scale_out_done"]` — a once-only flag that
rides in `exit_policy`. Nothing anywhere ever SET that flag. `_apply` in
`engine.py` executed the scale-out (fill, exits row, catastrophe re-sized
for the remainder — all correct) and moved on. Next bar, `scale_out_done`
was still false, target still met, fire again.

Fourth member of the same bug family as v0.12.5: a code path that could
never run live (bug #3 crashed `step()` before reaching it) and that the
test suite exercised with only a single bar — `test_05` verified one
scale-out fires correctly and never asked what happens on the bar after.

## The fix

`engine.py _apply`: after a `SCALE_OUT` whose outcome is `FILLED`, persist
`{"scale_out_done": True}` via `_update_policy` (in-memory + DB, same
mechanism the stop ratchets already use). A `REINSTATED` outcome (order
didn't fill inside the protection window) deliberately does NOT set the
flag — the scale-out retries on the next bar, which was already the
intended semantics.

## Tests

New `test_10d_scale_out_fires_exactly_once`: two consecutive bars at
target → exactly one TARGET exit row, flag persisted in `exit_policy`,
`qty_open` 60 → 30 once, second bar sells nothing.
`tests/integration/test_exit_engine_flow.py`: **13 passed**. Related unit
files: **41 passed**.

## Files changed

- `src/c4_exec/engine.py` — replaced (persist `scale_out_done` after a
  filled scale-out)
- `tests/integration/test_exit_engine_flow.py` — replaced (new regression
  test)
