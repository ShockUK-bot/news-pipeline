# Gate Lab review: what the shadow trades say, 2026-09-15

Read only. Source: `journal.gate_counterfactuals`, 2,581 rows (2,567 complete) from 2026-08-04 to 2026-09-15, the table behind the dashboard's Gate Lab tab (`dashboard/app.py`, `/api/gatelab`). Each row is a gate verdict that did not become a trade (a veto, an extended hours shadow `WOULD_TRADE`, a scanner candidate the analyst rejected, a shadow short), with the price at the verdict and the price 30 minutes, 2 hours and one session close later, plus the best and worst excursion. For a verdict after 15:00 CT the close is the next session's close. Real closed trades come from `journal.positions` and `journal.exits`.

All percentages below are **direction adjusted**: positive means the trade in the thesis direction would have made money by the close. Note that the Gate Lab tab's "EH shadow" scoreboard is not direction adjusted (it shows raw price change), so a bearish shadow that worked shows there as a negative number.

## 1. The three populations that matter

| Population | n | Avg to close | Win | Notes |
|---|---|---|---|---|
| Bullish news, evaluated at the open (08:30 to 10:00 CT), as a long | 160 | **−1.06%** | 36% | 6 of 6 weeks with enough rows closed lower on average |
| Bullish news, premarket, as a long (the extended hours shadow) | 817 | +0.16% | 47% | no edge; the live extended hours build the tab was meant to decide is not worth it for longs |
| Bearish news, premarket, as a short | 243 | **+0.62%** (median +1.63%) | 62% | 5 of 7 weeks positive, the last three +1.3%, +1.3%, +3.5% |
| Bearish news, after hours, as a short to next close | 206 | **+1.62%** | 59% | |
| Bearish news, 4 to 7% down at evaluation, as a short | 59 | **+3.77%** | 68% | the sweet spot |
| Bearish news, 12% or more down, as a short | 54 | −9.42% | 44% | capitulation bounces; the existing extended and SSR vetoes are right |
| Bullish news, 7 to 12% up at evaluation, as a long | 47 | −2.80% | 40% | |
| Bullish news, 12% or more up, as a long | 46 | −6.73% | 37% | |

Weekly split of the whole table: the down side has been positive in 5 of 7 weeks and every week since 08-31; the up side has been negative in each of the last four weeks.

## 2. What the live book confirms

- **News lane longs via `open_handoff`**: 18 closed trades, **−1,254**, 39% win, average −0.13R. These are not gap ups: gap at entry ranged from −4.3% to +0.9% and the premarket move was near zero, because the gate refuses priced in names. So the lane buys bullish news the market has not moved on, at the open, and holds overnight; 12 of 18 ended on the next day's overnight rule. The shadow population that matches it (bullish, premarket, flat) has no edge either (+0.16%, 47%). This is the single largest loser in the system and the counterfactuals say the edge was never there.
- **Scanner longs**: 12 closed, +449, 42% win (DELL carries it).
- **Shorts**: only three real ones ever (META, NTNX in August, CRCL on 09-15, +31 on the first live day of v0.14.8). Too few to say anything; the shadow data above is the evidence.

## 3. Candidate strategies, ranked by evidence

### A. Fade the bullish gap at the open (short)

Bullish news names that have already moved up 4 to 12% by the open fade into the close. Shorting them at the open, measured on the shadow rows evaluated between 08:30 and 10:00 CT:

| Gap at evaluation | n | Short avg to close | Win | Median adverse excursion | Win with a 5% stop |
|---|---|---|---|---|---|
| 4 to 7% | 15 | +1.30% | 67% | 2.2% | 67% |
| 7 to 12% | 13 | +3.99% | 77% | 1.7% | 77% |
| 12% or more | 14 | +3.22% | 50% | 6.2% | 29% |

The gate already identifies these names: `open_handoff` vetoes them as `PRICED_IN` (n=48 up; shorting them made +1.3% on average, 59% win) or `GATE_EXTENDED` (8% or more, n=15: +3.0%, 67%, median adverse 3.5%). The same effect shows in the premarket rows but with much wider adverse excursions (median 8%), so premarket entry is the wrong time; the open, after the first prints, is right. Above 12% the bounce risk dominates and the existing extended veto should stay.

**Shape of the lane:** a `fade` branch in C3 that turns a `PRICED_IN` or `GATE_EXTENDED` veto on a bullish thesis with 4 to 12% move into a SHORT candidate, ETB only, SSR veto kept, scalp style ladder (stop about 5%, breakeven, trail, force flat before the close, no overnight). Sample today is 28 trades in the actionable band, enough to justify a shadow build, not a live one. Run it in shadow mode (same `SHADOW_SHORT` machinery A3 already has) for two weeks to collect 40 or more rows with the real request shape, then flip.

### B. Bearish news short, evaluated before the open, entered at the open

The mirror of the losing long lane, and it works: bearish premarket news, shorted at the open, +0.62% average and +1.63% median to the close, 62% win, best in the 4 to 7% down band (+3.77%, 68%). Adverse excursion is wide (average 8% best case against you), so this wants the existing 2 ATR stop and a same day exit, not the overnight hold.

This lane **already exists** since v0.14.8 (shorting live, `open_handoff` for down theses) and the gate's vetoes on the down side are mostly right (`GATE_NO_CONFIRM` +0.07%, `GATE_EXTENDED` −1.5%, `SHORT_UNAVAILABLE` −2.0% intraday). Two places where it is pruned too hard:
- **Credibility on bearish news** (extended hours rows vetoed `CREDIBILITY`, down): n=70, +2.31%, 60% win. Bearish single source news moves stocks whether or not a second outlet confirms it. Candidate change: a lower corroboration requirement for down theses, measured first through the existing counterfactual rows.
- **Analyst rejects on scanner down movers**: n=35, +2.36%, 60% win. The analyst rejects 66% of all bearish theses (1,540 of 2,324 in 30 days). The scanner lane already skips A3 discretion for scalps; letting scanner down candidates that pass the structure checks reach A3 without an analyst veto is the same pattern.

### C. Stop, or shrink, the flat bullish open handoff long

Not a new strategy but the largest measured drag: the live lane is −1,254 over 18 trades and its shadow twin has no edge. Options: require a minimum premarket move (2 to 4%) so the lane only buys news the market is already confirming (the 2 to 4% up band at the open is the only up bucket with a positive shadow, and only just), or turn the lane's overnight hold off so a wrong entry costs a session, not two. Either is a config change to `gate.yaml` and `exit_profiles.yaml`; both are cheap to shadow first by reading the rows this table already has.

### Not supported by the data

- **Live extended hours long trading**, the question the Gate Lab was built to answer: no. Premarket bullish `WOULD_TRADE` rows: n=216, −0.37%, 37% win.
- **Buying capitulation** (12% or more down): average +9% bounce is driven by a few names; median −1%, 44% win. No.
- **Buying momentum on big bullish gaps**: 7% plus gaps close lower 60% of the time.

## 4. Recommended order

1. **Build A as a shadow lane** (no orders, `SHADOW_SHORT` rows plus counterfactuals), two weeks, then decide. It reuses the gate's existing veto detection, the short lane's sizing and the scalp ladder; the new code is one branch in C3 and a profile entry.
2. **Trim C now**: a minimum premarket move for the bullish `open_handoff` long, or overnight hold off for that lane. Zero new code; one number.
3. **Measure B's two pruning points** for another two weeks now that shorts are live, then relax credibility for down theses if the counterfactuals hold.

## 5. Caveats

- Counterfactual closes are single session and ignore slippage, borrow availability and fills; the fade lane in particular needs ETB confirmation at the open, which the short lane's precheck already does.
- The actionable fade band is 28 rows. The general effect (bullish at the open fades, n=160, six weeks) is solid; the exact band and stop are not yet.
- The Gate Lab tab's EH scoreboard should be made direction adjusted before anyone reads it for shorts; today it shows a working bearish shadow as a negative number.
