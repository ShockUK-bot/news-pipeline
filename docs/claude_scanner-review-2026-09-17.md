# Scanner lane review, all trades to 2026-09-17, and how to get more from it

Read only. No config changed, nothing restarted. All times America/Chicago. Data: `journal.positions`, `journal.exits`, `journal.position_events`, `journal.scanner_candidates`, `journal.decisions`, plus minute bar replays with `ops/tools/scalp_replay.py` and a variant of it (scratchpad, not committed). Supersedes the 09-13 review for the questions it re-examines.

## 1. Every scanner trade (17 filled; SMTC 08-26 never filled and is excluded)

| Date | Ticker | Side | Entry | Exit path | P&L $ | R | MFE R |
|---|---|---|---|---|---|---|---|
| 08-26 | INTU | LONG | 08:55 | target half 08:59, breakeven 09:39 (promoted to short_term) | +42.82 | +0.29 | 1.21 |
| 08-27 | CRM | LONG | 08:52 | target half 09:07, trail 09:28 | +294.67 | +2.23 | 3.02 |
| 08-27 | CRWD | LONG | 08:53 | target half 09:11, breakeven next day 08:37 (promoted) | +98.51 | +0.60 | 3.65 |
| 09-04 | KLAC | LONG | 08:58 | stop 09:27 | −105.07 | −0.78 | 0.52 |
| 09-08 | SKHY | LONG | 08:59 | time 09:59 | −11.48 | −0.09 | 0.66 |
| 09-09 | SKHY | LONG | 08:52 | stop 09:22 | −145.46 | −1.08 | 0.67 |
| 09-10 | SPCX | LONG | 08:58 | stop 09:06 (0.6R slippage) | −212.79 | −1.62 | 0.34 |
| 09-10 | AVAV | LONG | 09:27 | stop 09:30 | −119.21 | −0.75 | 0.37 |
| 09-11 | DELL | LONG | 08:57 | target half 09:00, trail 09:25 | +216.27 | +1.16 | 2.22 |
| 09-14 | CRWD | LONG | 08:52 | target half 08:57, promoted 11:33, trail 14:02 | +452.67 | +2.17 | 4.44 |
| 09-14 | PANW | LONG | 09:57 | time 10:57 | −62.40 | −0.27 | 0.49 |
| 09-15 | CRCL | SHORT | 08:52 | time 09:53 | +30.71 | +0.13 | 0.25 |
| 09-16 | SPCX | LONG | 08:53 | target half 09:15, trail 09:37 | +77.16 | +0.54 | 1.16 |
| 09-16 | CRCL | SHORT | 09:04 | target half 09:17, time 10:04 | +29.20 | +0.12 | 0.88 |
| 09-17 | GNRC | SHORT | 08:52 | target half 09:02, trail 09:05 | +353.60 | +2.16 | 2.57 |
| 09-17 | CRWV | SHORT | 08:53 | target half 09:07, trail 09:15 | +190.94 | +0.86 | 1.27 |
| 09-17 | SMCI | LONG | 09:36 | target half 09:43, trail 10:10 | +247.56 | +1.30 | 2.17 |

Totals: **+1,377.70 over 17 trades, 11 winners, 6 losers, +6.97R.** By week: 08-24 +436 (3 of 3), 08-31 −105 (0 of 1), 09-07 −273 (1 of 5), 09-14 +1,319 (7 of 8). Longs 13 trades, 7 winners, +773. Shorts (live since 09-14) 4 trades, 4 winners, +604, average +151. Entries 08:50 to 09:00: 13 trades, 9 winners, +1,283; the 09-13 worry that the first ten minutes are chop was a one week effect and is not supported by the full sample.

For comparison the news long lane is −1,254 over 18 trades. The scanner is the one live lane with a positive record, on a sample of 17, three quarters of the profit from the last four sessions.

## 2. What is actually limiting the lane

**Size, not selection.** Every trade is sized by A3 with `risk_per_trade_pct` 0.5 percent times the scanner `risk_multiplier` 0.5 (so 0.25 percent, about 250 dollars) and the global `max_position_notional_pct` 15 percent (about 15,000 dollars). The notional cap binds on 15 of 17 trades: notionals are 11,800 to 14,700 and the dollars at risk are only 131 to 246, average about 175, which is 0.17 percent of capital per trade. The 2 ATR(5m) stop on a large cap is about 1 percent of price, so a 15,000 dollar position cannot carry more than about 150 dollars of risk. Risk based sizing is not what is sizing these trades.

**The gate and the caps are not costing money.** Replays of every candidate that reached the analyst since 09-14 and did not trade:
- Concurrency cap (2 open scanner positions) blocked INTC, MRVL, MU and NOK at 08:54 today: +0.42R, −0.67R, −0.25R, −1.03R at the same ladder, net −1.5R. Correct veto.
- `SCANNER_STRUCTURE` vetoes: TEM +0.34R, SAIL +0.29R, BZ −0.09R, VRT −0.07R. Flat.
- MU 09-14 shadow short (shorting was still off for the scanner that morning): −0.41R.
- Analyst no trades (RETO +95 percent, FTFT +49 percent, SDGR at the top of its range): the right calls on their face.

**The exit ladder is close to right.** Replay of all 17 trades on Alpaca minute bars (base replay reproduces the real exits within a minute; totals below are the change against the base replay):

| Variant | Change vs current ladder | Where it comes from |
|---|---|---|
| No scale out (full size runs to the 1.5 ATR trail) | **+399** (+25%) | CRWD 09-14 +326, DELL +78, GNRC +61, SMCI +50, CRWD 08-27 +158; but CRCL 09-16 goes from −16 to −244 |
| Hold the runner half to force flat (no trail) | +122 | CRM +215; DELL −71, GNRC −61, SMCI −71 |
| No 60 minute time stop | +52 | PANW +221, CRCL 09-15 −276 |
| 3.0 ATR stop, same shares | +66 | SKHY 09-08 +107, PANW +221; CRCL 09-15 −276, worse per share on the real stop outs |
| Trail 2.5 ATR | −138 | every runner gives back more |
| Trail 3.0 ATR | −217 | same |

Only "no scale out" is a material gain, and it doubles the loss on the one trade that reversed after the target. Nothing here is a clear win on 17 trades; the 1.5 ATR trail is already the tightest and best of the trails tested.

**Promotion works.** CRWD on 09-14 was promoted from scalp to short_term at 11:33 and made 3.68R on the runner half by 14:02 instead of 0.6R. The two earlier promotions (INTU, CRWD 08-27) ended at breakeven. Keep it.

## 3. Recommendations, in order

1. **Lift the size, scanner lane only.** `risk.yaml` `scanner.risk_multiplier` 0.5 to 1.0 (the yaml itself says 1.0 after 30 closed trades; we are at 17 with 11 winners on a paper account) and a scanner specific notional cap of 25 percent so the risk cap can actually bind. The notional cap is global today (`limits.max_position_notional_pct`), so the lane specific cap is a small A3 change plus a yaml key, not just a number edit. Effect on the record: positions about 24,000 dollars with 250 to 500 at risk; the same 17 trades would have been roughly +2,300 instead of +1,378 and the losing week about −450 instead of −273. Worst case per day at the 5 trade cap is 2,500 dollars, 2.5 percent of capital, against the current 1.25 percent. Rollback is two yaml lines and an `a3-risk` restart.
2. **Journal the counterfactual per trade instead of replaying by hand.** `journal.trade_metrics` is empty. A nightly job (after force flat) that runs the replay variants for each scanner position closed that day and writes them there would make the "no scale out" and "3.0 ATR" questions answer themselves at 30 and 50 trades. This is the A11 loop the risk yaml comment refers to and it does not exist yet.
3. **Do not change the ladder, the gate, the concurrency cap or the entry timing now.** The evidence for each is either flat or favours what is running. Revisit "no scale out" at 30 trades with the journaled counterfactuals.
4. **Shorts deserve the same size as longs.** Four for four, average +151, all in liquid ETB names. Nothing in the sizing treats them differently today; item 1 covers them.
5. One design note for later: today three of the four capped names were AI semis moving together (INTC, MRVL, MU). A sector cluster rule (one position per theme) would matter more than a higher concurrency cap; today's replay says the cap saved money.
