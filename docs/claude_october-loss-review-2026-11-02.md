# Why the system lost money in October 2026, what is failing, and what to build next

Written 2026-11-02 (09:00 to 11:00 CT) from the live journal, the service logs and a full-market scan of October daily and 15 minute bars for all 5,233 listed US stocks (1,828 of them liquid: 20 million dollars a day or more, 5 dollars or more). Nothing in the system was changed for this review except the operator close of INVX at 09:01 CT. Companion documents: `claude_month-review-2026-10.md` (outages, operations) and `claude_current-state-2026-11-02.md`.

## 0. The verdict in one page

October: 38 closed trades, 17 winners, realized -1,693 (-5.4 R), equity 98,573 to 97,173. Since inception (2026-07-14) the account is down 3.1 percent. The market itself was mixed: SPY -0.9 percent, IWM +0.5, QQQ -6.7, ARKK -9.4. Growth and AI names sold off hard in the second half of the month.

The loss is not one bad rule. It is three lanes with no measured edge running at full size, plus exits that cut the one lane that works:

| Where the money went | October | Evidence |
|---|---|---|
| Thesis lane (5 longs, 0 winners) | -1,140 | AI infrastructure thesis bought RIOT, FRMI, RXT while they fell 30 to 38 percent in the month; no price check; LLM reviews said HOLD 52 times out of 58 |
| News shorts (9 trades, 3 winners) | -988 | The analyst's direction call has no predictive power (section 3.1); entries 70 to 234 minutes after the headline, at the open, after the move |
| Scanner first-minute stop outs (5 trades) | -2,097 | CRWV three times, STX, PURR: entered 09:50 to 09:52 ET with 1.6 to 2.4 percent stops, stopped inside 6 minutes |
| Scanner time stop (7 trades) | about -950 missed | The 60 minute time stop closed ASTS, IONQ, META, TSLA and NU; those names then moved an average 2.99 R further in our direction; the journaled "no time stop" variant turns the scanner's October from -244 to +707 |
| Scanner winners | +1,658 trail, +1,344 target | The scanner lane is still the only lane with a positive record: +1,648 since August over 44 trades |

The deeper problem is structural. The pipeline saw every one of October's 210 big movers (all had news, 177 were escalated, 176 reached the analyst) and traded 11 of them, mostly for a loss. The gates are built to avoid chasing (15 minute open blackout, "extended" veto above 6 percent, "priced in" veto when the gap is half the predicted move, earnings blackout, 60 minute time stop). What the market data for October says, measured only with information available at 09:45 ET (section 5.1), is that the one catalyst class with intraday follow through was the earnings reaction: a liquid stock up or down 8 percent or more at 09:45 on its earnings day kept going into the close 61 to 77 percent of the time for another 1.3 to 2.9 percent on average. Large non earnings up gaps did the opposite and faded. The system blacks out earnings and has no fade lane, so it trades neither. It is tuned to trade residual drift on ordinary headlines after the market has priced them, and there the LLM's direction call is a coin flip (section 3.1).

This is fixable, but not by tuning the current lanes. The plan in section 6 is: stop the two losing lanes this week (config only), remove the scanner time stop, then build the lane the data supports (day one continuation on large catalyst moves, including earnings), promote the two shadows that already measure positive (exhaustion fade, dilution shorts), and make the LLM a classifier rather than a direction oracle. Everything new goes through the journal as a shadow first, with a kill rule, before it is sized. I am not promising a profitable month; I am saying the current design cannot be profitable because its best trades are vetoed by construction, and the data points to a design that can.

## 1. The month in numbers

### 1.1 Account and market

| | |
|---|---|
| Equity 2026-10-01 | 98,573 |
| Equity 2026-10-31 | 97,173 |
| Equity 2026-11-02 09:00 | 96,996 |
| October closed trades | 38 (17 winners, 45 percent) |
| October realized | -1,693, -5.40 R |
| Average risk per trade | scanner 433, news 483, thesis 245 |
| SPY / QQQ / IWM / ARKK October | -0.9 / -6.7 / +0.5 / -9.4 percent |

Weekly: -977 (week of 09-28, including Oct 1 and 2), -22, +117, -313, -1,163.

Worst days: 10-26 -743 (CART -589 stop, TSLA -154 time), 10-15 -550 (CRWV, RIOT stops), 10-20 -473 (CRWV), 10-28 -479 (STX), 10-29 -290 (PURR). Best: 10-12 +681 (CBRS), 10-22 +323 (IREN), 10-30 +248 (LITE).

### 1.2 Lanes

| Lane | Oct trades | Winners | Realized | Sum R | Since inception |
|---|---|---|---|---|---|
| scanner short | 15 | 9 | +243 | +1.10 | +815 (20) |
| news long | 4 | 3 | +274 | +0.56 | -1,122 (30) |
| scanner long | 5 | 2 | -83 | -0.38 | +832 (24) |
| news short | 9 | 3 | -988 | -2.03 | -1,209 (21) |
| thesis long | 5 | 0 | -1,140 | -4.64 | -1,509 (10), now -1,670 with INVX |

### 1.3 Exit layers (October)

| Layer | Exits | Realized | Avg R | Price move in the 2 sessions after the exit |
|---|---|---|---|---|
| TRAIL | 9 | +1,658 | +0.83 | +0.48 R (left on the table, acceptable) |
| TARGET | 12 | +1,344 | +0.51 | |
| BREAKEVEN | 1 | +76 | +0.16 | +5.19 R (NBIS, closed at breakeven, then ran) |
| TIME | 7 | -179 | -0.06 | +2.99 R (the leak) |
| OVERNIGHT | 7 | -735 | -0.21 | 0.00 (neutral exit of a bad entry) |
| STOP | 11 | -3,780 | -0.91 | -0.28 R (stops were right to exit) |

Slippage is not the problem: STOP exits slipped 374 in total, TARGET and TRAIL gained 133 against their triggers.

## 2. Trade by trade dissection

### 2.1 Thesis lane: five longs, none ever in profit

| Trade | Entry | Stop | Result | MFE | Note |
|---|---|---|---|---|---|
| RIOT 09-25 | 23.05 | 18.94 | -239 | 0 R | thesis th-2026-001 "AI infrastructure lease pivot" |
| RIOT 10-05 | 19.15 | 14.73 | -278 | 0.13 R | re-entered 4 sessions after the stop |
| FRMI 09-24 | 4.76 | 3.73 | -249 | 0 R | third FRMI entry of the thesis |
| RXT 10-12 | 3.76 | 2.92 | -242 | 0 R | |
| INVX 09-17 | 29.89 | 25.97 | -131 | 0 R | exited on A6 review after 3 weeks |
| INVX 10-13 | 29.03 | 26.21 | -161 (closed today) | | re-entered 4 sessions after the review exit |
| RIOT 10-29 | 13.89 | 10.29 | open, -107 | | fourth RIOT entry; RIOT fell 36 percent in October |

Four of five October thesis trades never traded above entry (MFE 0). The thesis is a story the analyst (A5) wrote in August; the lane buys the "beneficiaries" at the open after a one day cool off, checks liquidity and a 10 percent "not extended" ceiling, but never checks that the stock is in an uptrend or that the sector is. Both theses with positions (AI infrastructure, small cap activism) were long growth in a month QQQ lost 6.7 percent and ARKK 9.4.

The oversight layers did not help. A6 nightly reviews on these seven positions: 58 verdicts, 52 HOLD. A12 guard verdicts on all October losers: 21, all HOLD. The LLM reads the original thesis, finds no headline that "invalidates" it, and says hold while the price falls 20 percent. A thesis that is wrong on price is not invalidated by news; it is invalidated by price, and nothing checks price.

### 2.2 News lane: the direction call is a coin flip, and the entry is late

The thirteen October news trades, with the catalyst and the delay from headline to order:

| Trade | Headline (source) | Published CT | Entered | Delay | Move before entry | Result |
|---|---|---|---|---|---|---|
| ZIM short | Israeli regulator halts Hapag-Lloyd review (Benzinga) | 07:05 | 08:47 | 102 min | +0.9 pct | -185 |
| NBIS short | Nebius acquires Inferize, terms undisclosed (Benzinga) | 06:06 | 08:51 | 165 min | -4.7 pct | -236 |
| ECHO long | 8-K (EDGAR) | 06:55 | 08:52 | 117 min | +3.2 pct | +251 |
| RIVN short | Q3 production, reaffirms guidance (Benzinga) | 07:33 | 09:01 | 88 min | -5.9 pct | -121 |
| OPCH short | trading halt, then McKesson buyout at 32.05 | 06:56 | 08:57 | 120 min | +10.3 pct | +11 (shorted a buyout target) |
| NUAI short | 8-K (EDGAR) | 05:02 | 08:56 | 234 min | -4.3 pct | +120 |
| RXRX long | 8-K (EDGAR) | 07:46 | 08:56 | 70 min | +3.3 pct | +136 |
| SLN short | trading halt (stock +126 percent on the day) | 06:56 | 08:45 | 110 min | +126 pct | 0 (guard exit) |
| CBRE long | 460 million pound acquisition (Benzinga) | 06:08 | 08:51 | 163 min | +4.2 pct | -118 |
| CWST long | Jana Partners stake (Benzinga, intraday) | 13:07 | 13:17 | 10 min | +1.4 pct | +6 |
| META short | depositary share offering (Benzinga) | 06:31 | 08:46 | 135 min | -3.5 pct | +102 |
| CART short | halts pricing experiments amid scrutiny (Benzinga) | 08:32 | 08:52 | 20 min | -1.7 pct | -589 |
| V short | potential 5 billion pound UK lawsuit (Benzinga) | 06:31 | 09:00 | 148 min | +0.8 pct | -88 (Visa rose 15 percent in October) |

The pipeline itself is fast (news to analyst verdict in 0 to 3 minutes). The delay is by design: the gate will not pass a pre-market story until 15 minutes after the open, with confirmation bars. By then NBIS had already fallen 4.7 percent, RIVN 5.9, META 3.5: we shorted the low of the gap and it bounced. The shorts' maximum favourable excursion averaged 0.17 R, maximum adverse 0.45 R; the predicted move (3.1 percent) was two thirds realized before we were in.

The catalysts themselves are weak. "Acquires company, terms undisclosed", "production in line, guidance reaffirmed", "potential lawsuit" are not events that move a stock for a day. The three winners were EDGAR 8-K filings (ECHO, RXRX, NUAI), where the filing is the primary event. Since inception the news lane is: Benzinga sourced -2,149 over 37 trades, EDGAR sourced -191 over 8, halts +11 over 2.

### 2.3 Scanner lane: right idea, wrong exits, wrong first two minutes

20 October scanner trades: +160 net (15 shorts +243, 5 longs -83). Every short but PURR and MAT was "with the move" on a down gapping AI or crypto name (CRWV, NBIS, IREN, ASTS, IONQ, CBRS, STX, LITE); in a month where QQQ fell 6.7 percent that was the right side.

The losses are concentrated in the session's first two minutes. The scanner's session starts at 09:50 ET; the first scan emits the most extended names and the gate passes them at 09:50 to 09:52:

| Trade | Entered ET | Stop | Stopped after | Loss |
|---|---|---|---|---|
| CRWV short 10-15 | 09:51 | 1.9 pct | 48 min | -408 |
| CRWV short 10-16 | 09:52 | 2.0 pct | 42 min | -395 |
| CRWV short 10-20 | 09:52 | 2.4 pct | 6 min | -524 |
| STX short 10-28 | 09:52 | 1.6 pct | 6 min | -479 |
| PURR short 10-29 | 09:50 | 1.6 pct | 2 min | -290 |

A 2 ATR(5 minute) stop at 09:50 on a stock that just gapped 6 to 10 percent is inside the normal noise of the first hour. The journaled "3.0 ATR stop" variant does not fix it (-529 against base over the 20 trades: the wider stop loses more on the same trades). What fixes it is not entering at 09:50 on the first print, and not shorting the same name three days running after two stop outs (CRWV: -1,327 in three tries).

The exits are the bigger leak. The 60 minute time stop (close if under 0.5 R after an hour) closed seven trades; those names then moved an average of 2.99 R further our way within two sessions (ASTS +4.6 R and +6.2 R, IONQ +2.7 R, META +4.5 R, TSLA +2.2 R). Across the 20 trades the journaled variants say: base -244, no time stop +707, 3.0 ATR trail +127, hold the runner +105, no scale out -195, 3.0 ATR stop -529, 2.5 ATR trail -360. A9 proposal 1 (drop the 50 percent scale out) is the smaller of the two exit changes; dropping the time stop is the larger and nobody has proposed it because A9 has no rule for it.

### 2.4 Overnight holds

Seven positions were held overnight because the 15:45 rule treats a one day old position as "young" and holds it; all seven were news trades that were already losing, and all were dumped the next afternoon for -735 in total. The post exit path is flat (0.00 R), so the exit was fine. The entries were the problem; the rule just delayed the loss a day and added gap risk.

## 3. What is failing, with the data behind each point

### 3.1 The analyst's direction call has no predictive power

The gate journals a counterfactual for every thesis it sees (price at the verdict, 30 minutes, 2 hours, close). That is 5,001 analyst direction calls since August scored against the close:

| Month | Direction | n | Average move in the called direction by the close | Right by the close |
|---|---|---|---|---|
| August | down | 483 | -0.05 pct | 55 pct |
| August | up | 1,525 | -0.07 pct | 48 pct |
| September | down | 735 | +1.27 pct | 60 pct |
| September | up | 465 | -0.89 pct | 40 pct |
| October | down | 1,068 | -1.15 pct | 51 pct |
| October | up | 725 | -0.49 pct | 49 pct |

Restricted to the 403 theses that passed every other gate (WOULD_TRADE rows since September): down calls +0.02 percent by the close, up calls -1.23 percent. By source: Benzinga headlines -0.75 (down) and -0.73 (up) percent; EDGAR +0.18 and -0.37; the one positive pocket is "down" calls on press releases (GlobeNewswire +3.27 percent, PR Newswire +3.18, n=90), which are dominated by offerings, reverse splits and bad earnings (section 4.4). Confidence does not separate: the 0.5 to 0.6 bucket, the most common, is -2.55 percent. Predicted magnitude above 5 percent is -0.97 percent, because by the time the analyst sees a big story the stock has already moved 3.8 percent and it mean reverts.

The model is a 27B parameter quantised Qwen reading a headline and a small context pack. It can tell you what the news is. It cannot tell you where the stock goes in the next six hours, and three months of journal say so. Every lane that uses its direction as the signal (news long, news short, thesis) is negative; the one lane that uses price as the signal and the model only as a filter (scanner) is positive.

### 3.2 The gates exclude the winners by construction

Of October's 210 liquid big movers (25 percent or more for the month, or 15 percent or more in a day):

| Stage | Count |
|---|---|
| had news items in the pipeline | 210 |
| triage escalated | 177 |
| analyst produced a thesis | 176 |
| gate PASS | 14 |
| scanner saw it | 124 (36 emitted) |
| traded | 11 |

The 11 traded: SLN (0), OPCH (+11, shorted a buyout target), NUAI (+120), PURR (-290), plus the scanner names already listed. The vetoes that removed the rest, in order: EH_LIQUIDITY (504: pre-market story, no two sided quote), CREDIBILITY (305: one outlet), SHORT_UNAVAILABLE (252), GATE_NO_CONFIRM (182: move under 1.5 percent or volume under 2.5x in the 30 minute window), PRICED_IN (116: gap at the open more than half the predicted move), GATE_EXTENDED (100: move over 6 percent), GATE_WINDOW (71: more than 30 minutes after the headline), SCANNER_STRUCTURE (42), risk SIZE_CLIPPED (20) and WIDE_SPREAD (29), EARNINGS_BLACKOUT (16), scanner EARNINGS_SOON (48) and MOVE_STALE_HOD (90).

Read those together: a story that moves a stock more than 6 percent is "extended", a gap that prices in the story is "priced in", anything outside the first 30 minutes after the headline is out of "window", earnings are blacked out, and a stock off its high for an hour is "stale". The gates were written to stop the system chasing. They do that. They also guarantee the system only trades the stories the market did not care about.

Were the gates wrong to veto? Mostly no, for the lanes they protect: vetoed CREDIBILITY "up" calls fell 2.2 percent by the close, vetoed SHORT_UNAVAILABLE "down" calls rose 5.5 percent on average (median +1.8), WOULD_TRADE shadows lost in both directions. The gates correctly stop the analyst's bad calls. The point is that the gates cannot create a good signal; they can only filter a bad one, and after filtering nothing is left (98 PASS from 2,197 theses, 36 orders).

### 3.3 The month's catalyst moves, measured without hindsight

A first pass at this question selected stocks by where they closed and found "three out of four big moves continued into the close". That was look ahead bias: picking days by the closing move guarantees the entry looks good, and the number is withdrawn. The clean measurement selects every liquid name whose price at 09:45 ET was 5 percent or more away from the previous close, using nothing known later, and follows a position taken with the move to the close. Section 5.1 has the full table. The short version:

| Move at 09:45 | n | With the move, 09:45 to close | Earnings day subset | Not earnings |
|---|---|---|---|---|
| 5 to 8 percent | 785 | +0.2 pct, 54 pct win: noise | +0.2 pct | +0.2 pct |
| 8 to 15 percent | 242 | +0.8 pct, 59 pct win | +1.3 pct, 61 pct win (n=64) | +0.6 pct |
| 15 percent or more | 77 | +0.3 pct, 56 pct: no edge overall | +2.9 pct, 77 pct win (n=22) | -0.8 pct; up gaps -2.9 pct, 27 pct win (n=26): a fade |

The scanner trades the first row and the bottom of the second with 1.6 to 2.4 percent stops at 09:50; the data says those moves are a coin flip at that moment and the median adverse excursion before the close is 2 percent, so the stop is inside the noise. The edge in October was in earnings reactions, which the system blacks out, and in fading very large non earnings up gaps, which the system has no lane for (the funnel counterfactual in 3.5 says the same thing about the scanner's rejected candidates).

What were the big movers? Buyouts (BRZE +52 percent day on Salesforce, SGRY +48, ADT +33, PTC +33, PCRX +44, RRC +18, OPCH +33: gap and done, the only trade is the deal spread and we shorted one of them), earnings (39 of the 132 one day moves of 15 percent or more were on the earnings date or the morning after: GKOS, TDOC, WING, BHC, TENB, CDNA, CROX, INTC, NOW, RDDT, SFM, ALNY, FMC), biotech binaries (SLN +126, LQDA, IRON, BHVN, AVBP -47, BCAX -44, IOVA -35), and a tail of small caps. October was earnings season: 1,444 reports in the calendar. The system has an earnings blackout in risk and an earnings filter in the scanner, so it sat out the month's main catalyst class on purpose and traded one earnings name (STX, the day after, for -479).

Two of the list look like corporate actions rather than trades and should be verified before anyone draws a lesson from them: CTVA -84 percent on 2026-10-01 (gap -81, the shape of a spin off) and XP (+31 then -48 with a -52 percent gap).

### 3.4 Sizing vetoes remove the liquid volatile names

20 SIZE_CLIPPED and 29 WIDE_SPREAD risk vetoes in October, on exactly the names that moved: AMD, INTC, CRWD, APLD, BE, IREN (twice), LITE, CRCL, ASTS. The scanner lane's 30 percent notional cap and the 0.5 percent risk budget with a 2 ATR stop cannot both be met on a 200 dollar stock with a 2 percent stop; the engine then refuses anything under half the risk budget. The spread cap (40 bps) rejects mid caps in the first hour. These two rules alone removed 19 down moves that the funnel counterfactual scores at +5.0 R long (fade) or -4.5 R short.

### 3.5 The scanner's veto and cap buckets are a fade signal nobody trades

A11 replays every scanner candidate that was emitted but not traded (vetoed, capped, rejected) through the scalp exits in both directions. October, 185 candidates:

| Group | n | With the move | Against the move (fade) |
|---|---|---|---|
| Down movers | 80 | -8.0 R, -10,323 | +14.5 R, +18,520 (56 of 80 winners) |
| Up movers | 105 | -7.8 R, -6,681 | +6.3 R, +3,394 (67 of 105) |
| September, same table | 110 | -5.1 R | +1.3 R |

Fading the candidates the structure, parabolic and liquidity rules reject, or that the per hour and per scan caps drop, made money two months running in the counterfactual, with 66 percent winners in October. A9 already watches this as "scanner early window" and "concurrency cap" rules; the bar it set (20 instances, +3 R, half winners) is met by the down mover fade on its own.

This does not contradict 3.3. The scanner candidates are 4 to 10 percent movers at 09:50 that fail a structure test (below VWAP for a long, bottom of the 30 minute range, vertical bar); those are the exhausted ones and they revert. The 15 percent plus movers in 3.3 are a different population and they continue. Both are real and both are measurable in the journal we already keep.

### 3.6 Earnings season was treated as noise

1,444 earnings reports in October. Risk vetoed 16 entries for the blackout, the scanner filtered 48 candidates for EARNINGS_SOON, and the analyst does not get told whether a headline is an earnings reaction. Earnings gaps are the most studied catalyst there is, and the pipeline has an earnings calendar, a news feed that carries the release, and a scanner that sees the gap. It has every input and a rule against using them.

### 3.7 The LLM oversight layers add cost and no protection

A12 guard: 36 October verdicts, 35 HOLD; classified outcomes 2 SAVE, 6 SHAKEOUT, 28 NEUTRAL. On the 18 October trades that lost more than 100, 21 guard verdicts, all HOLD. A6 nightly review on the thesis positions: 58 verdicts, 52 HOLD, the six EXIT recommendations all on INVX in its fourth week. Neither layer has prevented a single loss in October. They are prompted with the original thesis and asked whether the news invalidates it; news rarely does, so they always say hold. Price invalidates theses, and no layer reads price.

### 3.8 Operational and data items that cost money or coverage

- GlobeNewswire blocked since 10-08: about 600 items a day gone, the one source whose "down" calls had an edge (offerings and reverse splits).
- Exit cancel race (11 occurrences): worse fills on three RIOT attempts, and INVX left without its broker stop for two sessions (closed today).
- 10-05 host reset: eight minutes dark, no position hurt.
- Scanner "NO_TAPE" (404 filters) is by design: relative volume is undefined in the first 15 minutes, so the early window scans at 09:33 to 09:45 can only emit names with other data; this is why the early window shadow has thin numbers.

## 4. Top performing stocks of October and what the system did with them

Full tables are in the appendix. The pattern, by type of mover:

| Type | Examples | Did the pipeline see it | Why it did not trade it |
|---|---|---|---|
| Buyouts (gap to the deal price) | BRZE, SGRY, ADT, PTC, PCRX, RRC, OPCH | yes, all with 13 to 38 news items | correctly not a trade (OPCH was shorted into the buyout for +11; the halt feed plus "down" call produced a short on a takeover target) |
| Earnings winners | GKOS +34 day, TDOC +27, WING +12, BHC +29, CROX +26, CDNA +22, TENB | yes | EARNINGS_SOON (scanner), EARNINGS_BLACKOUT (risk), SCANNER_STRUCTURE, GATE_WINDOW |
| Earnings losers | INTC -29, NOW -27, RDDT -29, SFM -23, ALNY -25, FMC -31, FFIV -20 | yes (INTC 104 items, NOW 67) | same, plus SSR_RESTRICTED and GATE_NO_CONFIRM |
| Biotech binaries | SLN +126, IRON +39, LQDA +27, BHVN +25, AVBP -47, BCAX -44, IOVA -35 | yes | CREDIBILITY, GATE_EXTENDED, DOLLAR_VOLUME; SLN was shorted at +125 percent above the pre news price (flat exit) |
| Multi week trends | V +15 (we shorted it 10-29), ACN +42, EFX +35, HAS +36, FIGS +54, BBWI +43 | yes | no lane trades a trend; the thesis lane picked losers instead |
| Crypto and AI infrastructure collapse | CRWV -37, RIOT -36, CIFR -54, MARA -40, CLSK -40, IREN, FRMI -38 | yes | the scanner shorted CRWV, IREN, NBIS, LITE correctly; the thesis lane was long RIOT and FRMI the whole way down |

## 5. What the data says works

### 5.1 Earnings reaction continuation, and the non earnings gap fade (two new lanes)

Method: all 1,828 liquid names, every October session, 15 minute bars. A stock is selected when its price at 09:45 ET is 5 percent or more from the previous close (1,104 events). A position is taken with the move at the open of the 09:45, 10:00 or 10:30 bar and held to the 15:45 bar close. "Earn" means the name had an earnings report that day or the previous evening. MAE is the worst excursion against the position from entry to close, MFE the best, both in the move's direction. Fade results are the negatives of the with move results.

Entry 09:45 ET:

| Bucket | n | Mean | Median | Win | MAE median / 25th pct | MFE median |
|---|---|---|---|---|---|---|
| 5 to 8, all | 785 | +0.20 | +0.36 | 54 | -2.05 / -3.99 | +2.82 |
| 8 to 15, all | 242 | +0.77 | +0.96 | 59 | -2.47 / -4.51 | +3.40 |
| 8 to 15, earnings | 64 | +1.30 | +1.32 | 61 | -2.01 / -3.47 | +3.14 |
| 8 to 15, up, earnings | 32 | +1.16 | +1.48 | 66 | -2.40 / -3.68 | +2.89 |
| 8 to 15, down, earnings | 32 | +1.43 | +1.13 | 56 | -1.79 / -3.11 | +3.83 |
| 15 plus, all | 77 | +0.26 | +0.46 | 56 | -3.23 / -7.08 | +4.10 |
| 15 plus, earnings | 22 | +2.86 | +2.55 | 77 | -2.70 / -3.75 | +5.07 |
| 15 plus, up, not earnings | 26 | -2.89 | -1.37 | 27 | -5.43 / -10.88 | +1.64 |
| 15 plus, down, not earnings | 29 | +1.11 | +1.17 | 66 | -2.60 / -7.08 | +6.09 |

Entry 10:00 ET (the same buckets): 8 to 15 earnings +1.50 mean, 64 percent; 15 plus earnings +2.83, 73 percent; 15 plus up not earnings -2.13, 35 percent; 8 to 15 "pulled back toward the prior close by 10:00" +1.46, 66 percent (n=146) against "still extending" -0.32. Entry 10:30: earnings buckets still +0.9 to +2.4 percent and 62 to 77 percent; everything else is gone.

What this supports, and what it does not:

- Lane A, earnings reaction continuation: liquid name, earnings today or last night, 8 percent or more at 09:45, enter with the move at 09:45 to 10:00, stop about 4 percent (beyond the 25th percentile adverse excursion), hold to the close, trail from 2 R. October sample 86 events, mean +1.3 to +2.9 percent, 61 to 77 percent winners, median favourable excursion 3 to 5 percent. Both directions; shorts need ETB and no SSR, which the existing checks do.
- Lane B, non earnings large gap fade: liquid name, no earnings, up 15 percent or more at 09:45 with no buyout headline, short at 09:45 to 10:00, stop about 3 percent (the median favourable excursion of those moves after 09:45 was 1.6 percent), cover by the close. October sample 26, mean +2.9 percent, 73 percent winners. Small sample; shadow first.
- Entry timing for the scanner: on 8 to 15 percent non earnings movers, buying the first print at 09:45 to 09:50 is a coin flip and entering after a pull back by 10:00 is +1.5 percent and 66 percent; the current 09:50 session start with 2 ATR(5m) stops is the worst combination the table offers.
- Not supported: with the move on 5 to 8 percent movers (noise), holding anything overnight (next day drift is flat to negative), the current 60 minute time stop (median favourable excursion arrives later than an hour).

One month, one regime. These numbers set the shadow's success bar; they are not a forecast.

### 5.2 Exhaustion fade on rejected scanner candidates (promote the shadow)

Section 3.5: +20.8 R in October, +1.3 R in September, 66 percent winners, on the names the structure and parabolic rules reject and the caps drop. Entry rules already exist as veto rules; the lane is the inverse verdict with the scalp exits and no time stop.

### 5.3 Hold scanner winners (config change)

No time stop: +951 against base on October's 20 scanner trades; post exit drift after TIME exits +2.99 R; A9 proposal 1 (no scale out) adds a smaller amount; a 3.0 ATR trail is the best trail variant (+371 against base).

### 5.4 Dilution shorts (new lane, small)

193 analyst "down" calls since September on headlines containing offering, registered direct, private placement, ATM or convertible: 59 percent of those stocks were lower by the close, average fall 2.74 percent, median 1.01. They were mostly vetoed: SHORT_UNAVAILABLE 70 (micro caps, not borrowable: correct), CREDIBILITY 41 (one outlet: wrong for a primary source press release), EH_LIQUIDITY 23. An easy to borrow, 3 dollar plus, one outlet exempt version is the lane.

### 5.5 Things that worked and should be kept

The scanner shorts on down gapping names (+243 in a down tape), the credibility and borrow gates (both saved money), the broker resident catastrophe stop (never fired badly), the sizing discipline (no loss over 1.2 R), the journaling that made this review possible in two hours.

## 6. What to change, in order

Each item says what changes, the expected effect from the journal, and how we know if it worked. Nothing is applied automatically; each is a release with a shadow period where one is possible.

### Week 1 (config only, v0.27.0): stop the bleeding

1. Pause thesis entries: `config/thesis_entry.yaml` `max_new_per_day: 0`, keep the open RIOT under the existing exits. October effect: +1,140. Reopen only with a price rule (section 6, item 7).
2. Turn off news shorts from headline sources: `config/shorting.yaml` `lanes.news_short: false`, or restrict the news lane to EDGAR items and the dilution class. October effect: +988 on the shorts; the EDGAR longs stay.
3. Scanner exits: `config/exit_profiles.yaml` `scalp_v1.time_stop: null` (or window 240 minutes), trail k 1.5 to 3.0, and approve A9 proposal 1 (no scale out). October effect on the 20 trades: base -244 to about +700.
4. Scanner entries: session start 09:50 to 10:00 ET, and a per ticker rule of one attempt per day and no re-entry for three sessions after a stop out (CRWV). Effect: removes four of the five first minute stop outs (-1,700).
5. Keep the news long lane on EDGAR 8-K only, half size, as the measurement sample.

Expected: a scanner only system in October would have been about +700 to +1,300 instead of -1,693. That is not a profit plan; it is the baseline to build on.

### Weeks 2 to 4 (v0.28.0): the continuation lane, in shadow then live

6. New lane A "earnings reaction": universe is the liquid set (ADV 20 million plus, price 5 plus, easy to borrow for shorts) with an earnings report today or last evening in `news.earnings_calendar`. Trigger: at 09:45 ET the price is 8 percent or more from the previous close. Direction is the sign of the move, not the analyst's call; the analyst is asked only whether the headline is a one off print (buyout, reverse split, spin off) that must not be traded. Entry with the move at 09:45 to 10:00, stop 4 percent (about the 25th percentile adverse excursion in section 5.1), no time stop, trail from 2 R with 3.0 ATR(5m), flat at 15:50. Earnings allowed: this lane exists for them. Sizing through the existing risk engine with the notional cap raised for this lane. Shadow for two weeks via the WOULD_TRADE path and an A11 counterfactual (the funnel table already has the shape), success bar from section 5.1 (mean above +1 percent, 60 percent winners), then live with a three loss per day breaker and a 10 trade, -3 R kill rule in A9. Lane B "non earnings gap fade" (short the 15 percent plus non earnings up gap at 09:45 to 10:00, 3 percent stop, cover by the close) follows the same path one release later, because its sample is 26.

### Weeks 3 to 5 (v0.29.0): fade and dilution lanes

7. Exhaustion fade: the inverse of SCANNER_STRUCTURE and SCANNER_PARABOLIC verdicts plus capped candidates, scalp exits without the time stop, down movers first (the stronger half). It is already measured; go live at half size with the same kill rule.
8. Dilution shorts: offering class headlines from EDGAR (424B, 8-K item 3.02) and the wires, ETB only, price 3 plus, one outlet is enough when the issuer is the source, open handoff entry, short term exits.

### In parallel: fix the parts that are structurally wrong

9. Thesis lane rebuild before it reopens: a thesis beneficiary is bought only above its 20 day average with the sector ETF above its 20 day, one entry per ticker per thesis per 30 days, and the thesis itself is marked dead when its basket is down 15 percent from thesis creation. A5 keeps writing theses; price decides when they are tradeable.
10. Regime use: C8 already computes index trend, breadth and sector relative strength and nobody reads it. Rule: long lanes in a sector are off when the index is below its 50 day and the sector is in the bottom three; short lanes off in the mirror case. This alone would have kept the thesis lane out of AI infrastructure in October.
11. Oversight layers: A12 and A6 keep journaling, but their HOLD stops being an input to anything until their SAVE rate is measurably above zero. Replace with a deterministic rule: a thesis position below its initial stop level for two closes is exited at the next open, no LLM involved.
12. Sizing: lane specific notional cap (scanner and continuation 50 percent), minimum viable risk fraction 0.25 for names with ADV above 100 million, spread cap 60 bps for the first 30 minutes.
13. Measurement: A9 gets three new rules (lane kill at 10 trades and -3 R, time stop leak, first minute stop outs), the digest marks "new since yesterday", and every new lane starts with its counterfactual table so that the next review is a query, not a hunt.
14. Operations: the exit cancel race fix and the startup retry (v0.26.5), GlobeNewswire re-probe or back off, Google Drive token.

### What I am not recommending

Wider stops (the 3.0 ATR variant loses more), a bigger analyst model (the problem is the task, not the model size), pre-market trading (the edge in the data is after 09:45, not before), or any lane going live without a journaled shadow first.

## 7. Honest limits of this review

One month of data for the continuation pattern, two months for the fade, three for the analyst scoring. October was a growth sell off during earnings season; a quiet month will look different, which is why every new lane goes through the shadow path and a kill rule. Counterfactual fills are at the bar price with no slippage; the live scanner's slippage history says that costs 0.02 to 0.1 R a trade, not enough to flip the signs above but enough to shrink them. The account is paper; borrow and fill assumptions for shorts are optimistic.

## Appendix A: October's top movers (liquid names) and the pipeline's handling

Columns: October return, best or worst day, news items in the pipeline, analyst direction(s), gate result, scanner status, our trade.

### A.1 Top 30 gainers for the month

| Ticker | Oct pct | Best day | News | Analyst | Gate | Scanner | Traded | Earnings |
|---|---|---|---|---|---|---|---|---|
| BRZE | +82 | 10-20 +52 (Salesforce buyout) | 38 | down,up | | stale HOD, no tape, news owns it | | |
| SLN | +81 | 10-19 +126 | 22 | down,up | PASS | dollar volume | SHORT 0 | |
| SGRY | +67 | 10-26 +48 (buyout) | 13 | down,up | EXTENDED | emitted | | |
| ADT | +62 | 10-29 +33 (buyout) | 35 | down,up | WINDOW, PARABOLIC | emitted | | |
| GMAB | +60 | 10-28 +28 | 24 | down,up | STRUCTURE | emitted, spread | | |
| FIGS | +54 | 10-12 +12 | 6 | none | | | | |
| LQDA | +49 | 10-14 +27 | 43 | down,up | CREDIBILITY, SSR | capped per hour | | |
| RRC | +47 | 10-26 +18 (EQT buyout) | 25 | down,up | PRICED_IN, would trade | earnings soon | | 10-27 |
| TDOC | +47 | 10-29 +27 | 12 | down | STRUCTURE | emitted | | 10-28 |
| PCRX | +45 | 10-08 +44 (Viatris buyout) | 33 | down,up | CREDIBILITY, EXTENDED | dollar volume | | |
| GKOS | +45 | 10-29 +34 | 18 | down,up | EH_LIQUIDITY, STRUCTURE | capped, emitted | | 10-28 |
| BBWI | +43 | 10-05 +9 | 5 | none | | | | |
| WING | +42 | 10-28 +12 | 12 | down | WINDOW | earnings soon | | 10-28 |
| ACN | +42 | 10-01 +16 | 95 | down,up | EH_LIQUIDITY, NO_CONFIRM | earnings soon | | |
| OPCH | +42 | 10-06 +33 (buyout) | 30 | down,up | PASS, CREDIBILITY | news owns it | SHORT +11 | 10-29 |
| BHC | +41 | 10-29 +29 | 12 | up | EH_LIQUIDITY | dollar volume | | 10-28 |
| TENB | +39 | 10-26 +8 | 19 | up | | | | 10-28 |
| PTC | +39 | 10-05 +33 (Schneider buyout) | 30 | down,up | NO_CONFIRM | stale HOD | | |
| NTSK | +39 | 10-21 +10 | 10 | none | | | | |
| APPN | +39 | 10-15 +14 | 15 | up | NO_CONFIRM | | | |
| DXC | +38 | 10-15 +8 | 12 | down,up | CREDIBILITY, SSR | | | 10-29 |
| BLMN | +36 | 10-28 +10 | 6 | none | | | | |
| FCN | +36 | 10-29 +14 | 12 | down | WINDOW | | | 10-29 |
| HAS | +36 | 10-20 +11 | 26 | up | would trade | no tape | | |
| RPD | +36 | 10-26 +20 | 5 | none | | dollar volume | | |
| REAL | +35 | 10-15 +16 | 14 | down,up | EH_LIQUIDITY, EXTENDED | rel volume | | 10-29 |
| UPBD | +35 | 10-29 +18 | 16 | down,up | WINDOW | | | 10-29 |
| GLOB | +35 | 10-30 +7 | 9 | none | | | | |
| EFX | +35 | 10-20 +11 | 39 | down,up | | earnings soon, spread | | |
| CDNA | +34 | 10-30 +22 | 13 | up | | capped per hour | | 10-29 |

### A.2 Top 25 losers for the month

| Ticker | Oct pct | Worst day | ETB | News | Analyst | Gate | Scanner | Traded | Earnings |
|---|---|---|---|---|---|---|---|---|---|
| CTVA | -85 | 10-01 -84 (verify: spin off shape) | yes | 44 | down,up | CREDIBILITY | capped | | |
| NUAI | -59 | 10-14 -21 | no | 21 | down,up | PASS, SHORT_UNAVAILABLE | price floor | SHORT +120 | |
| SHAZ | -58 | 10-22 -16 | yes | 11 | down,up | | | | |
| GRML | -57 | 10-01 -14 | no | 17 | down,up | SHORT_UNAVAILABLE | emitted | | |
| BRUN | -57 | 10-14 -16 | yes | 15 | none | | no tape | | |
| CIFR | -54 | 10-13 -13 | yes | 44 | down,up | SSR, would trade | capped x3 | | |
| AVTX | -53 | 10-22 -39 | yes | 11 | down | | dollar volume | | |
| BCAX | -51 | 10-28 -44 | yes | 6 | down | | dollar volume | | |
| AVBP | -51 | 10-06 -47 | yes | 30 | down,up | CREDIBILITY, EXTENDED | dollar volume | | |
| CDNL | -50 | 10-09 -16 | yes | 16 | down,up | CREDIBILITY, EH_LIQUIDITY | dollar volume | | |
| SECZ | -48 | 10-12 -14 | no | 15 | down | | score floor | | |
| INTC | -45 | 10-30 -29 | yes | 104 | down,up | PASS, NO_CONFIRM, SSR | emitted, earnings soon | | 10-29 |
| AEHR | -45 | 10-22 -13 | yes | 41 | none | | | | |
| ACMR | -44 | 10-22 -10 | yes | 9 | up | CREDIBILITY | | | |
| IOVA | -43 | 10-29 -35 | yes | 15 | up | | stale HOD | | |
| QMCO | -42 | 10-16 -14 | yes | 19 | down | EH_LIQUIDITY | rel volume | | |
| FORM | -41 | 10-22 -15 | yes | 20 | down | | | | 10-28 |
| CLSK | -40 | 10-16 -10 | yes | 27 | up | | | | |
| RSI | -40 | 10-30 -11 | yes | 20 | up | | | | 10-28 |
| MARA | -40 | 10-14 -10 | yes | 21 | none | | move pct, rel volume | | |
| APUS | -40 | 10-08 -22 | no | 22 | down,up | SHORT_UNAVAILABLE | capped, emitted | | |
| GLND | -40 | 10-06 -19 | no | 9 | down | | price floor | | |
| UNIT | -40 | 10-14 -15 | yes | 2 | none | | | | |
| FRVO | -39 | 10-14 -9 | yes | 12 | down,up | | | | |
| UCTT | -39 | 10-22 -15 | yes | 9 | up | | | | 10-27 |

### A.3 Largest single day moves (liquid names)

Up: SLN +126 (shorted, 0), GLND +76, CLRO +69, APUS +55, BRZE +52, SGRY +48, PCRX +44, IRON +39, GKOS +34, PTC +33, ADT +33, OPCH +33 (shorted, +11), AXGN +31, XP +31, PCVX +31, FMC +31, BHC +29, GMAB +28, PRCH +28, TDOC +27, LQDA +27, CROX +26, BHVN +25, PURR +23 (shorted at 09:50, -290), REPL +23, HRI +23, RXO +23, CDNA +22, RHI +22, PAGS +21. Eighteen of the 58 were on an earnings date.

Down: CTVA -84 (verify), XP -48, AVBP -47, BCAX -44, CLRO -42, AVTX -39, ATEC -38, IOVA -35, AVEX -33, ORIC -32, FMC -31, RDDT -29, INTC -29, TTMI -28, NOW -27, SBS -26, INOD -25, KALU -25, PAGS -25, ALNY -25, INBX -23, SFM -23, LKQ -22, NKTR -22, APUS -22, FPS -22, CRS -21, NUAI -21 (shorted, +120), FFIV -20, CORT -20. Twenty one of the 74 were on an earnings date. None of the 74 was traded short except NUAI.
