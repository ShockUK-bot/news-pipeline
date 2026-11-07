# Patch notes v0.31.0 (2026-11-07): trend day mode

Built from `docs/claude_thu-fri-review-2026-11-07.md` on the operator's instruction. On Thursday 2026-11-05 QQQ was down 3.6 percent at 10:00 ET and closed down 6.4; every liquid name shorted at 09:45 made 2.4 to 4.6 percent by the close, and the system made two scratched trades because its caps and the Monday news short switch throttled it. Over two years the index rule "down 2 percent and still below the earlier price" was right 80 to 86 percent of the time. Trend day mode is that switch, wired to the single name lanes where the money was.

## How it works

- `src/common/trendday.py`, `config/trend_day.yaml`. From 10:00 ET the scanner (`c10-scanner`, every cycle) checks QQQ and SPY: previous close, the 09:45 ET price and the last quote. If either is down 2 percent or more and still below its 09:45 price, the day is a DOWN trend day: `journal.control.trend_day = "down@YYYY-MM-DD"`, an audit row, and a SYSTEM decision `TREND_DAY` with the numbers. The flag never clears during the session; tomorrow's date makes it inert. Only the down version exists: Friday 2026-11-06 showed up days are a coin flip.
- `c3-gate`: with the flag set, the news short lane switch is lifted for the session (shorting mode must still be `live`, ETB and SSR checks unchanged); a PASS for an up thesis becomes `VETO TREND_DAY_LONG` (journaled with its counterfactual); for with move scanner shorts the VWAP and range structure checks are relaxed (they vetoed PLTR, SPCX and MSTR on the 11:00 bounce).
- `a3-risk`: new LONG entries vetoed `TREND_DAY_LONG`; scanner concurrency 2 to 4, scanner position notional 0.30 to 0.15, gross short budget 0.30 to 0.60, short heat 0.015 to 0.03, short lane heat 0.02 to 0.03, portfolio heat 0.03 to 0.04; sizing rows carry the `TREND_DAY` flag.
- `c10-scanner`: down movers get per scan 4 (was 2) and per hour 12 (was 6).
- Open long positions are not touched; their exits run as normal.

Nothing changes on any other day: every consumer reads one control key and falls back to today's configuration when it is absent or stale.

## Files and tests

`config/trend_day.yaml`, `src/common/trendday.py`, `src/c10_scanner/service.py`, `src/c3_gate/service.py`, `src/c3_gate/rules.py` (`trend_day_long_filter`), `src/a3_risk/service.py`, `tests/unit/test_v0_31_0.py` (one v0.27.0 pin updated). Version 0.31.0. Suite 948 passed.

## Services

Restart `c3-gate`, `a3-risk`, `c10-scanner` (done Saturday 2026-11-07 with the market closed, watchdog timer paused). Rollback: `git reset --hard v0.30.2` and restart the same three, or `trend_day.enabled: false` and restart.

## What to watch

The first trend day will show as a `TREND_DAY` SYSTEM row and a `trend_day` key in `journal.control`; RISK rows with the `TREND_DAY` flag; `TREND_DAY_LONG` vetoes. A9 should get a rule that scores trend day sessions separately (not in this release).
