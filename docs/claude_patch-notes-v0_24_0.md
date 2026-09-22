# Patch notes v0.24.0 (2026-09-22, 16:00 CT): three shadow strategies, journal only

Operator direction after the 09-18 brainstorm: build the three best evidenced ideas as shadows. Nothing here places an order; every row lands in the journal, A11 scores it, A9 proposes when a bar is met. Also the SNDK review (below).

## 1. Bad news drift short (gate shadow, rule `drift_short`)

A bearish thesis the gate vetoes as PRICED_IN or GATE_EXTENDED, in session, past the open blackout, inside 240 minutes of the open, already down 1 to 15 percent from the pre news price, becomes a would-be short: the short side gate (ETB, SSR) is applied and the verdict is journaled as a GATE row with `rule='drift_short'` (WOULD_TRADE or the short veto) plus a `gate_counterfactuals` row the existing sweep completes with the close and the excursions. `src/c3_gate/rules.py` `drift_candidate` (pure), `src/c3_gate/service.py` `_drift_shadow` (the fade shadow pattern), `config/gate.yaml` `drift` block. Evidence behind it: 64 such vetoes in the 30 days to 09-18 closed about +0.9 percent lower on average, though outlier driven (one +32 percent day, one −11 percent), which is why the A9 bar below asks for the median and the session count too.

## 2. Bullish fade trigger widened (rule `fade`)

The v0.14.12 trigger (PRICED_IN or GATE_EXTENDED, 4 to 12 percent up, first 90 minutes) produced 2 rows in a week. The bullish vetoes cluster under 2 percent as GATE_NO_CONFIRM (40 rows) and HANDOFF_UNMOVED (7), and those closed lower too. Now: source vetoes PRICED_IN, GATE_EXTENDED, HANDOFF_UNMOVED, GATE_NO_CONFIRM; band 1 to 12 percent; window 180 minutes. `config/gate.yaml` `fade`, `fade_candidate` reads the sources from config.

## 3. Scanner early window, shadow (09:33 to 09:50 ET)

`config/scanner.yaml` `early_window: {enabled: true, start_et: "09:33"}`. The scanner now scans from 09:33 ET; inside the early window every survivor that would have been emitted is journaled `CAPPED / EARLY_WINDOW` instead, nothing is emitted, the live window still opens at 09:50. A11's funnel pass replays each early candidate both ways (`CAPPED_EARLY_WINDOW`). `src/c10_scanner/rules.py` `in_early_window`, `emission_disposition(early=True)`, `src/c10_scanner/service.py`. Note the relative volume estimate is noisier in the first minutes (pace against a small elapsed fraction); the rows carry it so the effect can be judged.

## A9 rules (`src/a9_review/candidates.py`)

- `rule_drift_short` and `rule_fade_lane`: 60 or more would-trades, average to the close at least +0.4 percent after a 10 bps cost, median above zero, win rate at least 55 percent, no more than 40 percent of sessions with a negative mean. Evidence pack `shadow_lanes` from the gate counterfactuals.
- `rule_scanner_early_window`: 20 or more early candidates whose with-move replays sum to +3R with at least half winners: propose opening the window at 09:33.

## Dashboard

Gate Lab: the fade panel is now "Shadow lanes" with a lane column, showing both fade and drift_short.

## SNDK review (2026-09-22)

The Rosenblatt coverage initiation was triaged at 06:43 and discarded by design (coverage initiations are negative category 1 in the A1 prompt). SNDK then ran from 1,757 at 09:30 ET to 1,847 by 09:40 and 1,892 by 09:45, entirely inside the scanner's 15 minute open blackout plus 5 minutes of settling. The scanner's first look at 09:51 bought 15 shares at 1,894.95, the top, and the 2 ATR stop took it out at 09:42 CT for −395.67. Replay: an entry at 09:35 ET would have made +0.37R (+430), at 09:40 +0.36R, at 09:45 +0.01R. Yes, the window is the limit, and shadow 3 measures exactly that from tomorrow. The triage rule on initiations is a separate design question for A1; not changed here.

## Tests and services

`tests/unit/test_v0_24_0.py` (widened fade, drift candidate cases, early window timing, disposition, A9 rules quiet and hot, yaml pins, service wiring). Suite 900 passed. Restarted after the close: `c3-gate`, `c10-scanner`, `c6-dashboard`.

## Rollback

`config/gate.yaml`: `drift.enabled: false`, fade block back to 0.04 / 90 / two sources; `config/scanner.yaml`: `early_window.enabled: false`; restart `c3-gate` and `c10-scanner`. Or `git reset --hard v0.23.0` on main and restart the three.
