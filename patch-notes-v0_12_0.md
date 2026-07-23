# v0.12.0 — TA context pack + trade-origin dashboard columns (2026-07-23)

First of the three momentum-scanner releases (design:
`claude/momentum-scanner-spec-v1_0.md`, operator-approved 2026-07-23).
This chunk deliberately touches NO gating or execution logic — it adds
information, not behavior: the analyst and guard now *see* technicals, and
the dashboard now answers "which input produced this trade / what did it
cost / what's it up or down in %".

Design change vs the spec: the PDT guard (spec §7) is DROPPED — the SEC
approved elimination of the FINRA pattern-day-trader $25k rule (effective
2026), and the account is above the old line regardless. Nothing to build.

## Changes

1. **TA context pack — `src/common/ta.py` (NEW).** Code-computed technical
   features over the existing MarketData protocol, injected into model
   context. Intraday: VWAP + % distance, ATR(14) on completed 5-minute
   bars, relative volume vs ADV(20) pace, position in day's range, gap %.
   Daily (260-bar fetch): Wilder RSI(14), SMA20/50 % distance, 20/50
   trend, % distance from the (up to) 52-week high with an honest
   `high_window_sessions`, 5-day return, ATR(14). Doctrine: null-safe
   everywhere — thin history, early session, off-hours, provider errors
   each degrade that field to null with a STABLE key shape; nothing raises,
   nothing vetoes (the v0.5.9/v0.11.10 lesson, applied from birth).
   The v0.12.1 scanner reuses these same functions.

2. **A2 analyst sees technicals** — `src/a2_analyst/context.py` adds
   `context.ta` (full pack); `src/a2_analyst/prompt.py` instructs the
   analyst to use it as *evidence* for the mandatory priced-in question
   and magnitude estimate (RSI 80 at the highs after a +9% week ≠ RSI 55
   in a flat base), and to treat null as "unavailable", never guess.

3. **A12 guard sees intraday technicals** — `src/a12_guard/context.py`
   adds `ta_intraday` (VWAP distance, ATR-5m, range position only —
   `intraday_only=True` skips the 260-bar daily fetch; the guard's
   question is "is the thesis broken NOW", so it keeps its deliberately
   smaller pack).

4. **`clock.session_open()`** — `src/common/clock.py` gains the 9:30-ET
   session-open helper (UTC out, None on weekends), same coarseness
   contract as `is_market_hours`. ET conversion stays in clock.py only.

5. **Position origin + open-position columns — migration 006 +
   dashboard.** `journal.positions.origin` (`'news'` default | `'scanner'`,
   stamped by C4 from v0.12.1 on; sympathy trades stay `'news'` — the axis
   is news-vs-tape, not first-vs-second order). `dash_positions` appends
   `origin`, `total_cost` (qty_open × avg_entry) and `pct_pnl` (computed
   in the view so C6 and A13 chat read identical numbers). C6 Open
   positions gains **Origin / Cost / P/L %** columns; Closed trades gains
   **Origin**; `/api/history` returns `origin`. NEWS chip = blue,
   SCAN chip = amber (none will show SCAN until v0.12.1).

## Files

NEW: `src/common/ta.py`, `tests/unit/test_ta.py`,
`schema/migrations/006-position-origin.sql`,
`patch-notes-v0_12_0.md`, `v0_12_0-deploy-guide.md`
REPLACED: `src/common/clock.py`, `src/a2_analyst/context.py`,
`src/a2_analyst/prompt.py`, `src/a12_guard/context.py`,
`dashboard/app.py`, `dashboard/index.html`

One DB migration (additive: one column with a default, one view replaced,
schema_meta row 6). No config-file changes. No new env lines. No new
services or timers.

## Tests

16 new unit tests (`test_ta.py`): RSI/SMA/trend/52-week-distance/5-day
return math and their thin-history nulls; 5-minute resampling excluding the
in-progress bucket; ATR-5m immaturity before ~75 min of tape; VWAP;
day-range position; relative-volume pace (incl. too-early null);
`session_open` weekday/weekend; pack shape stability under a fully dead
provider; `intraday_only` verified to skip the daily fetch. Full unit
suite: 273 passed in this build environment; the one failure
(`test_confidence_required`) reproduces UNCHANGED on the untouched v0.11.12
checkout here (env-sensitive, pre-existing — documented family).
Migration 006 applied against a fresh PG16 `journal` schema and verified
with a live row: origin defaults to `news`, `total_cost`/`pct_pnl` compute
correctly (100 sh @ $120 marked $126 → $12,000 / +5.00%).

## Rollback

`git checkout v0.11.12` + restart the three services. The migration can
stay (additive; the old view readers ignore appended columns and the old
code never references `origin`).
