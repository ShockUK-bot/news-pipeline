# Patch notes v0.30.0 (2026-11-02): deterministic analyst pre-check

## Why

October: the analyst (Qwen3.8 27B, 20 to 30 seconds a call) ran 8,354 times and rejected 6,343. About 1,100 of those rejects said "the stock has not moved since the news" and about 200 "the stock is halted". Both are facts the code knows before the model is called, and both are exactly what the gate would veto as GATE_NO_CONFIRM anyway. Analyst latency rose from 20 to 30 seconds across the month as earnings volume grew; this removes roughly a sixth of the calls and a larger share of the wasted ones.

## What changes

`src/a2_analyst/precheck.py` (pure): for a news-origin signal during the regular session (09:30 to 16:00 ET), if the price has moved less than `min_move_pct` (0.5 percent) from its pre-news reference:

- younger than `wait_max_min` (60 minutes) since the news: DEFER. The message goes back to the queue for `defer_secs` (300) with its attempt refunded (`common.queue.defer`, the same mechanism the gate uses); it is re-evaluated with fresh prices and no model call.
- older: REJECT, journaled as an ANALYST row with `payload.precheck = true`, `model_id` null, reason "pre-check: unmoved ... no model call".

Untouched: scanner-origin signals (the move is the signal), pre-market and after-hours news (no bars yet; the open handoff judges those), signals whose move or age is unknown. `src/a2_analyst/service.py` runs the check after the context is built and before the model call; `DeferAnalysis` in the consume loop maps to `queue.defer`. `config/a2.yaml` `precheck` block holds the numbers.

## Measurement

A11 and the dashboard see pre-check rejects as ANALYST REJECT rows with `payload->>'precheck' = 'true'`; count them against the model's own "has not moved" rejects to confirm the saving. Expected: about 1,300 fewer model calls a month, analyst p95 back under 35 seconds in earnings season.

## Files and tests

`src/a2_analyst/precheck.py`, `src/a2_analyst/service.py`, `config/a2.yaml`, `tests/unit/test_v0_30_0.py`. Version 0.30.0. Suite 940 passed.

## Services

`a2-analyst` restart (joins tonight's deploy with v0.27.0 to v0.29.1). Rollback: `git reset --hard v0.29.1` and restart `a2-analyst`, or `precheck.enabled: false` and restart.
