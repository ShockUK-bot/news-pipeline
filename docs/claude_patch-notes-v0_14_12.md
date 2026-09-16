# Patch notes v0.14.12

Gate Lab follow ups, 2026-09-15. Previous tag: `v0.14.11`. Source: `docs/claude_gatelab-review-2026-09-15.md`. Operator decision: build the fade lane as a shadow, apply the open handoff floor now.

## Change 1: open handoff long needs a minimum move (`HANDOFF_UNMOVED`)

`src/c3_gate/rules.py` `evaluate`, open handoff branch, after the PRICED_IN and GATE_EXTENDED checks: a bullish thesis is vetoed `HANDOFF_UNMOVED` when its signed move from pre news is below `gate.handoff_min_move_pct_long` (new, `config/gate.yaml`, 0.02). Down theses are exempt (the under 2 percent bearish band is the best populated positive bucket for shorts). 0 disables. The veto is journaled and measured in the counterfactual table like any other, so the cost of the floor is visible in the Gate Lab.

Why: the live open handoff long lane is 18 closed trades, −1,254, 39 percent win, and its shadow twin (817 premarket bullish rows) is +0.16 percent to the close, 47 percent win. The names were unmoved at the open by construction (the gate refuses priced in names) and had no edge. This is live from the `c3-gate` restart.

## Change 2: fade lane, shadow only

A final `PRICED_IN` or `GATE_EXTENDED` veto on a bullish thesis, evaluated between the open blackout and 90 minutes after the open, with the name up 4 to 12 percent from pre news, is re read as a would be SHORT:
- `rules.fade_candidate` (pure) decides; `C3Service._fade_shadow` applies the short side direction gate (ETB, SSR) for lane `fade`, writes a GATE decision with `rule='fade'` (`WOULD_TRADE`, or `VETO` with `SHORT_UNAVAILABLE` / `SSR_RESTRICTED` / `LONG_ONLY`), and records a counterfactual row (`direction='down'`, `rule='fade'`) for the existing sweep. Nothing is enqueued; A3 never sees it; the branch is wrapped so a failure can never affect the gate.
- `config/gate.yaml` `fade:` block (`enabled`, `min_move_pct` 0.04, `max_move_pct` 0.12, `window_max_min_after_open` 90). `config/shorting.yaml` `lanes.fade: true` only enables the ETB and SSR checks for the lane; the shadow is unconditional.

Why: Gate Lab, the 4 to 12 percent band at the open shorted would have made +1.3 to +4.0 percent to the close with 67 to 77 percent win and a median adverse excursion under 2.5 percent (n=28). Above 12 percent the bounce wins; premarket entries have three times the adverse excursion. The lane needs about 40 rows with the real request shape before a live build, which also needs an A3 sizing path (profile for origin `fade`, scalp style ladder, force flat). Estimated volume from the last five weeks is in the deploy record.

## Tests

`tests/unit/test_v0_14_12.py`, 15 tests: the floor (vetoes, passes, bearish exempt, 0 disables, ordering after PRICED_IN and GATE_EXTENDED), `fade_candidate` (in band, intraday inside window, below band, capitulation band, outside window, other vetoes and passes, bearish, disabled) and config pins including a source check that the shadow branch contains no `enqueue(`. Suite: 817 passed, 1 skipped.

## Dashboard note

The Gate Lab RTH view groups by veto reason for every rule except `eh_shadow`, so fade rows appear there as `WOULD_TRADE` and the short vetoes; the EH scoreboard is still not direction adjusted. Both are cosmetic and left for a dashboard release; the review queries in the Gate Lab review document read the table directly.

## Services

`c3-gate` only (reads `gate.yaml` and `shorting.yaml` at startup). Evening restart, watchdog timer paused. A3 unaffected.

## Verification

- `c3-gate` active, `config version active` for the v0.14.12 commit, no tracebacks.
- Next session: `HANDOFF_UNMOVED` vetoes appear in `journal.decisions` for flat bullish open handoffs; `rule='fade'` rows appear in `journal.gate_counterfactuals` on gap up mornings and complete after the close.

## Rollback

`git reset --hard v0.14.11` on `main`, restart `c3-gate`. Or set `handoff_min_move_pct_long: 0` and `fade.enabled: false` in `gate.yaml` and restart `c3-gate`.

## Expected effect (measured on the last five weeks before deploy)

- The floor would have vetoed **32 of the 33** open handoff long passes since 08-11 as `HANDOFF_UNMOVED`. The lane effectively goes quiet until bullish news arrives that the market has already moved on by 2 percent or more. That is the intent: the data says the unmoved version had no edge. If the lane should stay more active, lower the floor to 0.01 and measure.
- Fade band rows ran **2 to 6 per week** (bullish PRICED_IN or GATE_EXTENDED between 08:45 and 10:00 CT with a 4 to 12 percent move). At that rate 40 rows is nearer eight weeks than two; the first read should be at 20 rows, about a month, and the band or window widened only if the effect holds.

## Deploy record

(filled in after the restart)
