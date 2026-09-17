# Patch notes v0.17.1 (2026-09-17, 17:15 CT): A11 guard classification refined

First A11 reading said A12's HOLD verdicts were 48 shakeouts to 16 saves. Cutting those by the position's unrealised R at the verdict showed 21 of the 48 were on positions already more than 1R in profit (15 of them one CRWD position at +2.3R), where the later give back is the trailing stop's design, not the guard's call. The guard's output space is risk reducing only and the news was benign.

- `src/a11_metrics/metrics.py` `classify_guard()`: a HOLD on a position at or above +1.0R at the verdict is journaled `NEUTRAL`, with the give back still recorded in `outcome_pnl_r` so the ladder question stays measurable. EXIT and TIGHTEN_STOP verdicts unchanged.
- `src/a11_metrics/service.py`: reads the unrealised R at verdict from the guard decision payload; new `--reclassify-guard` flag re-runs the rule over every row.
- Reclassified all 157 rows at 17:15 CT. New mix: HOLD 16 saves, 27 shakeouts (12 of them FRMI, one thesis position that bled for weeks while each item was correctly judged noise), 104 neutral. EXIT: 3 saves, 1 shakeout, 6 neutral.

Read: the guard is fine. Its exits are right or harmless, its holds are a coin flip on losers and correct on winners. No prompt change. The FRMI pattern (a wide stopped thesis position receiving many hold verdicts while bleeding) is the A6 review to C11 exit bridge's job, now with the v0.16.1 sale at the open. Auto execution of EXIT verdicts stays off until the sample is bigger than 10.

No services touched (A11 is a timer). Suite 862 passed.
