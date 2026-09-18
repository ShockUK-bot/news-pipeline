# Patch notes v0.23.0 (2026-09-18, 16:15 CT): scanner funnel counterfactuals

Operator request after the 09-18 review (COIN blocked twice, once by the concurrency cap and once because the analyst proposed a short the gate vetoed). Every scanner candidate that is emitted or capped but never trades is now replayed in both directions and journaled, so the two open questions get evidence instead of anecdotes: does the concurrency cap cost money, and should the analyst be shorting scanner up moves.

## What it does

- `src/a11_metrics/funnel.py`, run inside the nightly A11 (`--funnel-days`, default 14): for each untraded candidate, classify the outcome from its decision chain (`CAPPED_CONCURRENT`, `CAPPED_PER_SCAN`, `CAPPED_PER_HOUR`, `GATE_VETO` with reason, `RISK_VETO` with reason, `ANALYST_REJECT`, `SHADOW_SHORT`, `NO_DECISION`), record the analyst's proposed direction, then replay a `scalp_v1` trade from the detection minute both long and short on Alpaca minute bars (2 ATR(5m) stop, same ladder as the live lane, shares at the lane's 29k notional). Written to `journal.scanner_funnel_cf` (migration 021, applied 16:10 CT), one row per candidate. `with_move_r` is the trade in the scanner's direction.
- A11's report prints the new rows and a 30 day summary; A9's evidence pack gains `funnel`, with two new rules: raise the concurrency cap to 3 when 10 or more capped entries would have made +3R with the move; fix the scanner lane to the move's direction when, over 8 or more cases, the with-move long beats the analyst's proposed short by 2R.

## Backfill, 57 candidates since 09-04

| Outcome | n | With the move | Long | Short |
|---|---|---|---|---|
| Gate structure veto | 17 | 0.00R | +0.91R | −0.08R |
| Capped per scan | 10 | −2.79R | −1.95R | +2.16R |
| Analyst no trade | 9 | +0.75R | +0.75R | −0.33R |
| Shadow short (pre 09-14) | 8 | −1.97R | +0.13R | −0.39R |
| Size clipped | 5 | −0.86R | −0.16R | +0.07R |
| Capped concurrent | 4 | −1.21R | −1.21R | +0.69R |
| Capped per hour | 3 | −0.92R | −0.92R | +0.17R |

Read so far: every cap and veto is saving money or neutral. The concurrency cap's four blocked entries net −1.21R even with COIN's +0.89R in them. Analyst shorts on scanner up moves: 24 cases, the proposed shorts made +0.07R in total against +1.15R for the with-move longs, the long better in 9 of 24. That is a 1.1R gap on a 2R bar, so it stays a watch item; A9 will say when it crosses.

## Tests and services

`tests/unit/test_v0_23_0.py` (classification, the two rules on quiet and hot evidence, wiring). Suite 894 passed. A11 run once through its unit after the migration (57 rows). No long running service touched. Known cosmetic: on that first run the "by outcome" table in the A11 log printed empty while the helper returns it correctly; checked on the next scheduled run.

## Rollback

Disable nothing; the pass is additive. `git reset --hard v0.22.1` on main removes it; the table can stay.
