# v0.12.5 — Exit engine actually runs on live bars; overnight holds don't freeze (2026-07-24)

**Fixes the frozen JNJ "Last price" found on the morning of 2026-07-24 —
and the far more serious bug the diagnosis uncovered underneath it: on
every LIVE minute bar, the exit engine crashed right after writing the
mark, so no stop / trail / invalidation / overnight logic has ever
evaluated on real market data.** Three code changes, two new regression
tests. No schema change, no config change, no systemd change. One service
restart (`c4-exec`). **Deploy during market hours is recommended — until
this is in, open positions are protected by the broker catastrophe stop
only.**

## What was actually wrong

Position 5 (JNJ, opened 2026-07-23) was the system's first position held
overnight. The morning after, its dashboard price was frozen. Diagnosis
found three stacked bugs:

1. **The halt heuristic measured staleness in wall-clock time**
   (`engine.py check_halt`). It freezes a position when no bar has arrived
   for 10 minutes during market hours — a proxy for a real trading halt.
   But it compared raw timestamps, so an overnight hold was "571 minutes
   stale" 47 seconds after the next open and got frozen instantly:

   ```
   08:30:47 WARNING c4.engine msg='halt heuristic froze position' position_id=5 ticker=JNJ
   ```

2. **A frozen position could never resume** (`service.py engine_loop`).
   The loop skipped the bar fetch entirely for frozen positions
   (`if await engine.check_halt(pos): continue`), but the freeze is only
   cleared inside `step()`, which only runs when a bar is fetched. Frozen
   once, frozen until a service restart.

3. **`step()` raised TypeError on every real bar** (`engine.py`). Both
   marketdata adapters (Alpaca and Fake) return `bar["ts"]` as a
   `datetime`; `step()` did `int(bar["ts"])`, which throws. The crash
   landed AFTER the mark-to-market write but BEFORE every exit layer, so
   the dashboard price looked alive while stops, trails, invalidations,
   the 15:45 overnight pass, and the scalp force-flat check silently never
   ran (`ERROR c4.service msg='engine loop error' error=TypeError(...)`
   once per minute). The test suite never caught it because the test bar
   helper hardcodes an integer epoch — a datetime-shaped input never
   reached `step()` in tests. This also explains why C4 journaled no
   OVERNIGHT_HOLD_DECISION for JNJ on 2026-07-23 (only A6's advisory ran).

**Protection while broken:** the broker-resident catastrophe stop was
armed and live the whole time — the account was never exposed to a large
gap unprotected. But the tighter synthetic stop and every software exit
layer were not being enforced on live data.

## The fix

1. **`engine.py check_halt` — session-aware staleness.** The staleness
   clock now starts no earlier than today's 09:30 ET open, so overnight
   and weekend gaps contribute nothing. A genuine ≥10-minute in-session
   bar gap (a real halt) still freezes, including one starting at the open.

2. **`service.py engine_loop` — frozen positions still fetch bars.**
   `check_halt` still journals `HALT_FROZEN` and flags the position, but
   no longer blocks the bar fetch. In a real halt the feed returns no bars
   and the position stays safely frozen; when bars flow again, `step()`
   clears the freeze and journals `HALT_RESUMED` — the resume path now
   exists in production.

3. **`engine.py step()` — accepts any bar timestamp shape.** `datetime`,
   epoch number, or missing all work; live bars evaluate end-to-end.

## Known issue, NOT fixed here (follow-up)

Every arm attempt for position 5 journals
`ARM FAILED: MIPError('UNRESOLVABLE_REF: prenews_price')` — the
`close_below_prenews` invalidation cannot resolve the pre-news reference
price for this position (its other predicate arms fine). Present since the
position opened; separate data/reference issue to diagnose next.

## Tests

- `test_10b_overnight_hold_is_not_a_halt` — overnight gap does not freeze;
  a real 12-minute in-session gap the same morning still does.
- `test_10c_step_accepts_datetime_bar_ts` — `step()` with an Alpaca-shaped
  datetime timestamp completes and writes the mark.

`tests/integration/test_exit_engine_flow.py`: **12 passed**. Related unit
files (`test_exit_engine.py`, `test_scalp_exits.py`): **41 passed**.

## Files changed

- `src/c4_exec/engine.py` — replaced (session-aware `check_halt`;
  timestamp-tolerant `step()`)
- `src/c4_exec/service.py` — replaced (no skip before bar fetch)
- `tests/integration/test_exit_engine_flow.py` — replaced (two new
  regression tests)
