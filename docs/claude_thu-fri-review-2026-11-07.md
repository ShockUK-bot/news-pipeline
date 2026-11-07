# Why the system did not short Thursday 2026-11-05 or buy Friday 2026-11-06, with back tests

Written Saturday 2026-11-07 from the journal, the service logs and Alpaca bars (all 1,827 liquid names, 15 minute bars for both days; SPY and QQQ 15 minute bars for two years).

## 1. What the market did

| | Thursday 11-05 | Friday 11-06 |
|---|---|---|
| SPY | open -0.2 pct, 09:45 -1.3, 10:00 -1.7, close -4.4 | open +0.3, 10:00 +1.0, close +0.3 |
| QQQ | open -1.3, 09:45 -3.0, 10:00 -3.6, close -6.4 | open +1.8, 10:00 +2.4, close +1.4 |
| SMH (semis) | close -11.2 | close +3.6 |
| VXX | +28 pct | flat |

Thursday was a trend day: the index fell from the first minute to the last, with one bounce between 11:00 and 11:30. Friday was a thirty minute bounce. The high of the day for SPY and QQQ was the 10:00 bar; from 10:00 to the close SPY fell 0.7 percent and QQQ 1.0 percent. Friday did not regain "a lot" of Thursday: SPY recovered 0.3 of 4.4 points, QQQ 1.4 of 6.4. Anyone buying the bounce after 10:00 lost money on the day.

## 2. What the pipeline did

Thursday: 1,048 triage escalations (double a normal day), 294 analyst theses of which 244 were "down". Trades: scanner short TSLA at 10:03 ET (+149) and scanner short AVGO at 10:48 (-103), both closed by the breakeven ratchet inside 20 minutes. Realized +46 on the day. Friday: no trades, 68 gate vetoes, realized 0.

Where the Thursday shorts went, stage by stage:

| Stage | What happened | Evidence |
|---|---|---|
| News lane, 244 down theses | 147 vetoed LONG_ONLY because news shorts were switched off on Monday (v0.27.0) | The 59 vetoed during the session would have fallen a further 3.06 percent on average by the close (median 2.27, 40 of 59 fell): about 11,700 at the lane's 6,500 notional. The 88 vetoed after the close are not tradeable |
| Scanner, 44 down movers seen | 29 capped (12 early window, 10 per hour, 7 per scan), 9 emitted | Funnel replay of the 26 capped names shorted with the move: +14.5 R, +12,600 at lane size, 26 of 26 winners |
| Scanner, 9 emitted down movers | 2 traded (TSLA, AVGO), 3 SIZE_CLIPPED (NVDA, TSM, INTC), 3 SCANNER_STRUCTURE (PLTR, SPCX, MSTR), 1 SSR | NVDA, TSM and INTC fell 5.5, 5.4 and 4.1 percent from 09:45 to the close. The structure vetoes at 11:02 to 11:12 read the 11:00 bounce as "above VWAP"; the three closed below their detection price |
| Risk sizing | SIZE_CLIPPED with binding clip `gross_short` | `shorting.max_gross_short_notional_pct 0.30` is the same number as `risk.scanner.max_position_notional_pct 0.30`: the TSLA short alone used the whole gross short budget, so NVDA, TSM and INTC were sized to 22, 0 and 79 shares and vetoed as not viable |
| Exits | Both shorts scratched at breakeven | TSLA: breakeven ratchet moved to entry six seconds after the fill (first bar progress read 0.95 R), stopped on the 10:12 bounce; TSLA then fell from 350 to 344. AVGO: ratchet at 0.76 R after five minutes, stopped at 11:11; AVGO then fell from 298 to 292 |
| Index ETFs | 43 falling ETFs filtered ETF_EXCLUDED | By design: the scanner trades single names. There is no index lane |

Friday: the scanner emitted 7 up movers (IREN +21 percent, AMD +12, MU +9, FROG +27, SNDK, BE, FLY), all vetoed SCANNER_STRUCTURE at 09:45 to 09:50 because they were already below VWAP (fading). The funnel replay says the vetoes were right for that population: longs on Friday's emitted and capped up movers -12,200 at lane size, shorts +10,350. The news lane had 32 up theses; the 10 vetoed and measured fell 3.8 to 4.9 percent by the close. Friday's 38 vetoed news shorts (LONG_ONLY) would still have made +1.63 percent on average (20 of 38).

Two further findings from the week:

- The catalyst shadow lanes journaled nothing: the `scanner_candidates` status constraint does not allow SHADOW, every insert failed, fixed by migration 023 (v0.30.2, needs the operator's yes to run).
- A9's Saturday review proposed opening the scanner at 09:33 ET (proposal 3, +5.6 R over 37 early window candidates) and reports the exhaustion fade buckets: up movers vetoed by structure, shorted, +1.8 R over 29; down movers capped or clipped, bought, -6.1 R over 94 (Thursday alone was -21 R for that bucket). The fade must never be a blanket rule.

## 3. Back test: every liquid name, Thursday and Friday

Selection at 09:45 ET by move versus the previous close; position held from the 09:45 or 10:00 bar open to the 15:45 close; no stops.

Thursday (1,827 names):

| Bucket at 09:45 | n | Long to close, mean | Long wins | Short to close, mean |
|---|---|---|---|---|
| down 8 pct or more | 111 | -4.3 pct | 16 pct | +4.3 |
| down 5 to 8 | 134 | -4.6 | 9 pct | +4.6 |
| down 2 to 5 | 255 | -3.3 | 11 pct | +3.3 |
| flat (within 2) | 1,168 | -2.8 | 8 pct | +2.8 |
| up 2 to 5 | 112 | -2.4 | 14 pct | +2.4 |
| up 8 or more | 24 | -1.7 | 33 pct | +1.7 |

Everything fell. Shorting anything at 09:45 and covering at the close made 2 to 5 percent; the early movers (down 5 to 8 percent at 09:45) continued the most. Entering at 10:00 instead of 09:45 gave up about a third of it (down 5 to 8: +3.0 instead of +4.6).

Friday (1,826 names):

| Bucket at 09:45 | n | Long to close, mean | Long wins |
|---|---|---|---|
| up 8 or more | 61 | +2.3 pct (median +1.2) | 57 pct |
| up 5 to 8 | 85 | +1.6 (median +0.8) | 58 pct |
| up 2 to 5 | 191 | +1.1 | 58 pct |
| flat | 1,171 | +0.4 | 55 pct |
| down 2 to 5 | 281 | +0.7 | 65 pct |
| down 8 or more | 18 | -2.4 | 33 pct |

Friday longs were mildly positive on average and barely better than a coin flip; the mean is carried by a few names (FROG +7 percent from 09:45). The emitted names (AMD, MU, IREN) all closed below their 09:45 price. "Long Friday" in single names was a small edge at 09:45 and none at 10:00; "short Thursday" was a large edge everywhere.

## 4. Back test: index ETF rules, SPY and QQQ, October 2024 to November 2026 (528 sessions)

| Rule | SPY | QQQ |
|---|---|---|
| A: gap of 1 pct or more, trade with the gap from 10:00 to the close, only when 10:00 confirms | n=26, mean -0.19, win 58 pct | n=47, mean -0.10, win 55 pct |
| B: fade a 1 pct gap from 10:00 | n=49, mean +0.14, win 47 pct | n=104, mean -0.01, win 46 pct |
| D: down 2 pct or more at 09:45, short to the close | n=4, mean +0.29, win 75 pct | n=11, mean -0.04, win 64 pct |
| E: down 2 pct or more at 11:00 and below the 10:00 price, short to the close | n=7, mean +0.48, win 86 pct | n=15, mean +0.27, win 80 pct |
| F: day after a 3 pct drop, long open to close | n=4, mean +0.46 | n=11, mean -0.24, win 27 pct |
| F2: day after a 2 pct drop, long open to close | n=12, mean +0.32, win 58 pct | n=29, mean +0.26, win 62 pct |
| G: day after a 3 pct drop, long from 10:00 | n=4, mean -0.22 | n=11, mean -0.52, win 36 pct |

Thursday under these rules: QQQ 10:00 to close -2.9 percent (rule A short would have made +2.9, rule E +1.5 from 11:00). Friday: QQQ 10:00 to close -1.0 percent (rule G long loses 1.0; rule F long from the open +0.1).

Conclusion from two years: an index gap and go rule has no edge (rule A is negative on both ETFs), index fades have none either, and "buy the day after a crash" is positive only when you buy the open, not the 10:00 bounce, and only after 2 percent drops (62 percent winners, +0.26 average), not after 3 percent ones. The one rule that held up is E, the trend day short confirmed at 11:00 (80 to 86 percent winners, +0.3 to +0.5 percent per trade), on 7 to 15 instances in two years. Ten trades a year at half a percent on an index ETF is not a business; it is a filter. Its real value is as a switch for the single name lanes, which is where Thursday's money was.

## 5. What the system is and is not designed to do

It is a single name catalyst system with a two position scanner and a long only news lane since Monday. It is not designed to express an index view, and the data says it should not try to: the index rules above are weak, while Thursday's single name shorts were strong and uniform. The gap is not a missing index lane; it is that on a trend day the system's safety caps (2 concurrent positions, 2 per scan, 6 per hour, a 30 percent gross short cap equal to one position, structure vetoes that read bounces, a 0.75 R breakeven ratchet) throttle it to two scratched trades, and the lane that would have shorted 59 news names was switched off on Monday because it lost money in October's ordinary tape.

## 6. Proposed changes, in order of evidence

1. Run migration 023 so the shadow lanes measure (needs your yes; Saturday, nothing trades).
2. Trend day mode (new, deterministic): when QQQ or SPY is down 2 percent or more at 10:00 ET and below its 09:45 price, for the rest of that session: news shorts on (the LONG_ONLY switch lifted for the day), scanner concurrency 2 to 4 and per hour 6 to 12 for shorts only, no new longs, structure veto suspended for with move shorts. Mirror for up 2 percent days is NOT supported by the data (Friday longs were a coin flip; rule D' negative) and is not proposed. Thursday under this mode: roughly 59 news shorts at +3 percent and 8 to 10 more scanner shorts at +3 to +4 percent instead of two scratches.
3. Fix the gross short budget: `shorting.max_gross_short_notional_pct 0.30` to 0.60 with `risk.scanner.max_position_notional_pct` 0.30 to 0.15, so one scalp cannot block the next two. Config only.
4. Breakeven ratchet: measure first (v0.30.2 adds be1.5 and nobe variants to the nightly replay); if nobe or be1.5 beats base over two weeks, change scalp_v1. Also fix the mechanics so the first bar after a fill cannot count as progress.
5. A9 proposal 3 (scanner from 09:33 ET): the early window names shorted on Thursday were the best bucket (+6.5 R over 10); over four weeks +5.6 R over 37 with 65 percent winners. I would approve it together with change 2, because the early window on a normal day is where the stop outs came from in October.
6. Keep the exhaustion fade off: the down mover fade lost 21 R on Thursday alone.

Nothing above is applied. Items 1, 3 and 5 are config or migration; item 2 is a release (C3 and A3 read one regime flag that C8 or C3 computes from the SPY and QQQ snapshot at 10:00).
