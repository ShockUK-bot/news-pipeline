# Patch notes v0.28.0 (2026-11-02): catalyst shadow lanes (journal only)

Built the same day as v0.27.0 from section 5.1 of `docs/claude_october-loss-review-2026-11-02.md`. No order path. Two lanes are measured in the journal from tomorrow's open; A9 holds the go-live bar.

## What it does

Between 09:45 and 10:05 ET the scanner (`c10-scanner`) takes one extra look at the movers and most actives universe. A liquid name (ADV 20 million plus, price 5 plus, spread 60 bps or less, no ETF or warrant) is journaled to `journal.scanner_candidates` with `status='SHADOW'` and one of two reasons:

- `EARNINGS_CONTINUATION`: the name reported earnings today or after yesterday's close (`news.earnings_calendar`; new helper `reported_recently` in `src/c1_ingestion/earnings.py`) and is 8 percent or more from the previous close, either direction. The shadow trade is WITH the move.
- `GAP_FADE`: no earnings, up 15 percent or more. The shadow trade is the SHORT.

Each name is looked at once per day (the study's selection is the 09:45 print). Nothing is emitted, no analyst call, no order. `catalyst_shadow.mode` exists in `config/scanner.yaml` but only `shadow` is implemented; any other value logs a warning and still only journals.

A11's nightly funnel pass (`src/a11_metrics/funnel.py`) now includes SHADOW rows and replays them with the lane's own ladder instead of the scalp one: fixed percent stop (4 percent, 3 percent for the fade), no time stop, no scale out, breakeven at 1 R, a 3.0 x ATR(5m) trail from 2 R, flat at 15:50 ET (`replay_hold` in `ops/tools/scalp_replay.py`). Rows land in `journal.scanner_funnel_cf` with `outcome` `SHADOW_EARNINGS_CONTINUATION` or `SHADOW_GAP_FADE`; `summary` adds long and short winner counts.

A9 (`src/a9_review/candidates.py`) gets two rules. Watch list lines until 20 rows exist; a proposal when the lane's side shows at least +5 R summed and 55 percent winners over the four week window. The proposal is "go live at half size", which needs the live order path release (A3 profile, C3 rule, C4 fixed percent stop); nothing flips on its own.

## Files

`config/scanner.yaml` (`catalyst_shadow` block), `src/c10_scanner/rules.py` (`in_catalyst_window`, `catalyst_shadow_lane`), `src/c10_scanner/service.py` (`catalyst_shadow_pass`, called after each `scan_once`), `src/c1_ingestion/earnings.py` (`reported_recently`), `ops/tools/scalp_replay.py` (`replay_hold`), `src/a11_metrics/funnel.py`, `src/a9_review/candidates.py`, `tests/unit/test_v0_28_0.py`. Version 0.28.0. Suite 928 passed.

## Services

`c10-scanner` restart (same evening as v0.27.0's restarts). `a11-metrics` and `a9-review` are timers and pick the code up on their next run. Rollback: `git reset --hard v0.27.0` (or v0.26.4 to undo both releases) and restart `c10-scanner`.

## What to expect

October would have produced about 86 earnings rows and 26 gap fade rows, so the 20 row bar should be reached in one to two weeks for the earnings lane and three to five weeks for the fade. The first Saturday review (2026-11-07) will show the watch lines; the evening digest does not list shadow rows.
