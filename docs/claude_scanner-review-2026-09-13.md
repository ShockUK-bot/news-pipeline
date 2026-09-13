# Scanner profitability review, 2026-09-04 to 2026-09-13

Read only review of every CLOSED position opened since 2026-09-04, scanner lane first, news lane for comparison. All times America/Chicago. Data from `journal.positions`, `journal.position_events`, `journal.decisions` and `journal.scanner_candidates`. No config was changed.

Window context: v0.14.5 went live 2026-09-03 evening (scanner large cap volume tier and liquidity scoring term). Six market sessions: 09-04, 09-08, 09-09, 09-10, 09-11 (09-07 was Labor Day, 09-12 and 09-13 weekend). All 6 scanner entries and all 4 news entries fall in this window. Account equity about 97,400.

## 1. Scanner lane: 6 closed positions

| Ticker | Date | Entry (time) | Exit (time) | Exit reason | Hold | P&L $ | P&L % | P&L R | MFE R | Score | RelVol | ADV20 $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| KLAC | 09-04 | 185.40 (08:58) | 184.07 (09:27) | STOP | 29 min | -105.07 | -0.72 | -0.78 | 0.51 | 0.601 | 2.78x | 1.47bn |
| SKHY | 09-08 | 187.75 (08:59) | 187.60 (09:59) | TIME (60 min, under 0.5R) | 60 min | -11.48 | -0.08 | -0.09 | 0.66 | 0.621 | 4.11x | 3.15bn |
| SKHY | 09-09 | 195.28 (08:53) | 193.34 (09:23) | STOP | 30 min | -145.46 | -0.99 | -1.08 | 0.67 | 0.616 | 4.43x | 3.34bn |
| SPCX | 09-10 | 154.23 (08:59) | 151.97 (09:07) | STOP | 8 min | -212.79 | -1.47 | -1.62 | 0.34 | 0.604 | 4.29x | 11.43bn |
| AVAV | 09-10 | 158.60 (09:27) | 157.29 (09:30) | STOP | 3 min | -119.21 | -0.83 | -0.75 | 0.37 | 0.624* | 13.93x | 0.23bn |
| DELL | 09-11 | 551.35 (08:57) | 557.00 x13 (09:01) then 562.33 x13 (09:25) | TARGET scale out, then TRAIL | 28 min | +216.27 | +1.51 | +1.16 | 2.28 | 0.632 | 4.02x | 4.16bn |

Scanner total: **-377.74** over 6 trades, 1 winner, 5 losers, sum -3.16R, average -0.53R per trade. Profile `scalp_v1` on every trade: stop 2.0 x 5 minute ATR, breakeven at 0.75R, 50 percent scale out at the 0.6 target, trail 1.5 ATR from 1R, 60 minute time stop, force flat 15:50 ET. MFE is the high water mark in R before exit.

Notes on individual trades:
- **SPCX** stop was 152.83 and filled at 151.97, so 0.6R of the 1.62R loss was slippage past the stop, in an 11bn ADV name. Worth watching stop fill quality separately.
- **AVAV** (*) was journaled at 0.624 but its true score under the current formula is about 0.77. Cause: `src/c10_scanner/rules.py` line 276 reads `m.minutes_since_extreme or 60`, so a candidate exactly at its day high (0 minutes) is treated as unknown and gets zero freshness credit instead of full credit. It is the only emitted candidate in the window that was at its extreme, and no SCORE_FLOOR reject was affected, so it changed no outcome here. It is still a bug in the direction the floor is meant to reward and needs a one line fix plus a unit test in a future release.
- **DELL** is the only trade where the move continued after entry (MFE 2.28R). It had the highest score of the six, the biggest move (8.3 percent), and had been holding near its high for 6 minutes at detection.

### What the funnel says about these six

Between 09-04 and 09-11 the scanner emitted 32 candidates. 19 were down movers; every one of those became a THESIS/SHORT and then either a gate VETO (10), a shadow short (7, shorting lane off for scanner) or an analyst NO_TRADE. 13 were up movers; 6 became positions, 3 were vetoed at RISK on SIZE_CLIPPED (META, MRVL, BKNG), 1 vetoed at gate (HPQ), 3 analyst NO_TRADE. So the live scanner book in this window is long only, and the six entries are the entire long sample.

Two things stand out:

1. **Five of the six entries only existed because of v0.14.5.** KLAC, SKHY, SKHY, SPCX and DELL are all large cap tier names (ADV20 over 1bn). Recomputing their scores with the pre v0.14.5 weights (rel volume 0.45, no liquidity term) gives 0.51, 0.53, 0.53, 0.52 and 0.54, all under the 0.60 floor. The new 0.15 liquidity term, which is at or near 1.0 for these names, is what lifted them to 0.60 to 0.63. Those five trades net -258.53 (four losses, DELL the win). AVAV, the one small cap entry with a 13.9x volume multiple, would have scored 0.85 under the old weights and lost as well.
2. **The 2.0x large cap volume bar admitted exactly one trade: KLAC at 2.78x, a loss.** Every other entry cleared the standard 3.0x bar on its own. In the same window 46 large cap candidates were rejected on REL_VOLUME at 0.65x to 1.99x, so the 2.0x bar is sitting just above where the mega caps actually print (MU, AMD, INTC, AVGO, TSLA, MSTR all appear at 1.2x to 1.99x). MSTR, the case that motivated the tier, was rejected three more times in this window at 1.85x, 1.91x and 1.32x. The tier has not yet caught the setup it was built for.

Timing pattern: five of six entries were filled between 08:52 and 08:59, inside the first ten minutes of the scanner session (opens 08:50, after the 15 minute open blackout plus 5 minutes settling). The four stop outs came within 3 to 30 minutes with MFE of only 0.34R to 0.67R before reversing. That reads like first hour chop hitting 2 ATR(5m) stops that are about 1 percent away, not like a scoring problem.

## 2. News lane: 4 closed positions in the same window

| Ticker | Date | Entry (time) | Exit (time) | Exit reason | Hold | P&L $ | P&L % | P&L R | MFE R | Catalyst | Gate at entry |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BEN | 09-08 | 34.58 (08:47) | 34.17 (09-09 14:45) | OVERNIGHT eod rule | 30h | -136.12 | -1.19 | -0.28 | 0.25 | M&A, majority stake purchase | open_handoff, vol 1.50x, price -0.5% vs prenews |
| BMY | 09-08 | 64.80 (08:50) | 64.35 (09-09 14:45) | OVERNIGHT eod rule | 30h | -78.75 | -0.69 | -0.16 | 0.22 | Phase 2 results, endpoints met | open_handoff, vol 1.63x, price -2.0% vs prenews |
| PLTR | 09-08 | 171.19 (08:59) | 173.07 x16 (09:22) then 170.00 x16 (09-09 14:45) | TARGET scale out, then OVERNIGHT | 30h | +11.04 | +0.20 | +0.02 | 0.15 | Strategic partnership (NBIS) | open_handoff, vol 0.50x, price -1.3% vs prenews |
| SLG | 09-09 | 52.56 (08:52) | 51.58 (09-10 14:45) | OVERNIGHT eod rule | 30h | -141.72 | -1.87 | -0.29 | 0.06 | 226M asset sale | open_handoff, vol 0.76x, price -0.7% vs prenews |

News total: **-345.55** over 4 trades, 1 marginal winner, sum -0.71R, average -0.18R per trade. Profile `short_term_v1`: stop 2.0 x daily ATR(14), breakeven at 1R, scale out 50 percent at the 0.7 target, trail 2.5 ATR from 1.5R, 2 session time stop, overnight rule `eod_rule_v1`.

The news lane has no scanner score or relative volume. The nearest equivalents are the gate numbers above: all four came through the `open_handoff` rule (premarket news, entered at the open), all four were trading below their pre news price at entry, and volume multiples were 0.5x to 1.6x.

One pattern worth a separate look: on every one of the four, the `close_below_prenews` invalidation fired on the session close pass at 15:01 to 15:03 CT, the exit order could not fill in 45 seconds because the market had closed, the catastrophe stop was re placed, and the position was then held until the next day's 14:45 overnight rule exit. So a known invalidation was carried for another full session each time. That is behaving as designed (session predicates evaluate after the close), but it means the invalidation currently has no way to act before the next day's 14:45.

## 3. Scanner versus news, same window

| | Scanner | News |
|---|---|---|
| Trades | 6 | 4 |
| Winners | 1 | 1 (marginal) |
| Total P&L | -377.74 | -345.55 |
| Sum R | -3.16 | -0.71 |
| Average hold | 26 min | 30 h |
| Typical loss | 0.75R to 1.6R (stop) | 0.16R to 0.29R (overnight rule) |

Dollar losses are similar; the R numbers are not comparable across lanes because the news R unit is a daily ATR and the scanner R unit is a 5 minute ATR. Both lanes were net losers this week. Neither lane has enough trades for the numbers to mean much on their own.

## 4. My read on the 0.60 score floor and the 2.0x large cap bar

**The 0.60 floor: leave it.** Within the long sample the score has no visible discriminating power: the winner scored 0.632, the losers 0.601 to 0.624. Raising the floor to 0.65 would have removed all six trades including DELL; lowering it would have admitted the 28 large cap SCORE_FLOOR rejects at 0.49 to 0.59, which look like the same kind of name at the same kind of move. The real change in this window was not the floor but the liquidity term that pushed a cluster of large caps from 0.52 to 0.61. What the six trades actually say is that large cap 4 to 8 percent movers entered at 09:50 ET with a 1 percent stop got chopped out four times in five. That is an entry timing and stop width question, and I would look there before touching the score.

**The 2.0x large cap bar: leave it, but note it is not yet doing its job.** It admitted one trade (KLAC, a loss) and continued to reject the mega caps it was designed for, MSTR included, because they print 1.2x to 1.99x. Dropping it to 1.5x would admit roughly 20 more large cap candidates a week to scoring, where most would then meet the 0.60 floor. I would not do that on a sample of one.

**What I would want before changing either:**

1. **A real sample.** At least 30 scanner long entries, or 6 to 8 weeks at the current rate. Six trades and one winner cannot separate a bad week from a bad rule.
2. **Forward returns for the rejected pool.** The 37 SCORE_FLOOR and 76 REL_VOLUME rows in this window are journaled with full metrics. Attaching a 30 minute, 60 minute and force flat return to each FILTERED row (the A9 counterfactual loop the yaml comments already describe) would show whether the 0.55 to 0.60 band and the 1.5x to 2.0x band actually behave worse than what was emitted. That is the only way to tune either number from evidence rather than anecdote.
3. **The freshness bug fixed first.** The `or 60` on line 276 of `rules.py` zeroes the freshness credit for candidates at their day extreme, which are exactly the ones the floor should favour. Fix it, add a test for `minutes_since_extreme == 0`, and let the scores settle before judging the floor.
4. **Split the timing question from the score question.** Five of six entries in the first ten minutes of the session, with MFE under 0.7R before the stop. Try measuring what the same candidates did from 10:15 or 10:30 ET entries, or whether a wider first hour stop changes the loss shape, before concluding the candidates were wrong.
5. **Stop fill quality.** SPCX slipped 0.6R past its stop in an 11bn name. If that repeats, the effective risk per scalp trade is larger than the sizing assumes.
6. **A shadow short readout.** Seven scanner down movers went to shadow this week. Their paper outcomes would double the scanner evidence at no cost and are already in the journal.

Nothing above needs a config change today. The scanner breaker (3 losses per day) did not trip in the window; the worst day was 09-10 with two.

## Files

- This review: `docs/claude_scanner-review-2026-09-13.md`
- Open code item to carry forward: `src/c10_scanner/rules.py` line 276 freshness credit for `minutes_since_extreme == 0`.
