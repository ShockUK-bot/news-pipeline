# BURST session 2 review and recommendations (2026-09-17)

Read only. Companion to `claude_burst-target-review-2026-09-16.md` (session 1, why 1 percent was wrong) and `claude_patch-notes-v0_15_3.md` (bracket sweep). All times America/Chicago. Rule naming: a `fade` row's `direction` is the trade direction, so `fade down` is a short against an up burst and `fade up` is a buy against a down burst.

## 1. Session 2 in numbers

84 detections (session 1 had 158 on FOMC day), 73 scored, all 73 carry the five bracket sweep, so v0.15.3 works. One news anchored burst. Spreads 9 bps average. The heaviest hours were 09:00 and 14:00 (17 each).

Bracket sweep, session 2 only (first hit within 30 minutes, entry at the detection print, cost 10 bps):

| Rule | n | 0.5 / 0.5 | 0.7 / 0.5 | 1.0 / 0.7 | 1.0 / 1.0 | 0.5 / 0.35 |
|---|---|---|---|---|---|---|
| fade down (short an up burst) | 42 | 64% target, 19% stop, **+0.126%** | +0.095% | +0.040% | +0.114% | +0.073% |
| fade up (buy a down burst) | 31 | 58% target, 29% stop, **+0.045%** | −0.094% | −0.081% | −0.100% | +0.011% |
| momentum (either side) | 73 | −0.25 to −0.33% | −0.27 to −0.32% | −0.06 to −0.35% | −0.10 to −0.31% | −0.37% |

So 0.5 / 0.5 is the best bracket on both fade sides, as the session 1 percentiles predicted, and momentum loses on every bracket for the second day running.

## 2. The problem: the edge is inside the first minute

Two sessions combined, fade rows, return in the trade's direction:

| Entry assumed at | 30 min return, session 1 (FOMC) | 30 min return, session 2 (ordinary) |
|---|---|---|
| the detection print | +0.29% | +0.23% |
| the price one minute after detection | +0.21% | **+0.06%** |

Of today's 45 fade rows that hit the 0.5 percent target, 31 hit it inside the first minute after detection. Excluding those, shorts are 7 targets against 8 stops and longs 7 against 9: no edge. Bursts over 1.5 percent look like the best fades (+1.2 percent at 30 minutes) but the whole move happens in the first minute (+1.21 percent at one minute, −0.01 percent from minute one to thirty), which means the detection print is the spike itself. A real order sent after detection fills a minute later at the bid or ask, after the snap back.

Other cuts, both sessions, fade rows, 30 minute return from the one minute price: 10:00 to 13:00 is the only window near the bar (+0.29 percent, n=78); before 10:00 is +0.09 to +0.20 (n=50); 13:00 to 15:00 +0.07 (n=98). Volume multiple 5 to 8x is best (+0.26), over 12x is negative (−0.02). Spread buckets are flat. A second burst in the same symbol within 30 minutes of the first is a loser (n=14, −0.20 percent). Names the scanner already knows fade worse (+0.02, n=43) than the rest (+0.20, n=183).

## 3. Verdict

Not a lane. At a realistic entry the ordinary session shows +0.06 percent before cost, below the +0.15 percent after cost bar. The scoring is currently flattering the fade because it assumes a fill at the burst's own last print.

## 4. Recommendations

1. **v0.15.4, scoring honesty (small, C12 only, research path).** Score the path from a realistic entry: the first trade at least 30 seconds after detection, with half the quoted spread added as crossing cost, kept alongside the current "at print" numbers so the two can be compared. Exclude a repeat burst in the same symbol within 30 minutes from the fade population (journal it, do not score it as a candidate). Cap fade candidates at 1.5 percent burst size (larger is news, VRTX +6.9 percent today). `burst_report.py` prints both entry conventions. No trading impact, restart `c12-burst` after the close.
2. **Keep measuring.** The go live bar stands (200 scored rows, +0.15 percent after cost at the realistic entry, no more than 40 percent of sessions negative). Two sessions is not a decision either way; what has changed is that the number to watch is the realistic entry column, not the print column.
3. **If it ever becomes a lane it is the 10:00 to 13:00 window, up bursts only, 5 to 8x volume, first burst in the name that half hour, 0.5 / 0.5 bracket, 10 minute time stop.** That is the shape the data points at; the sample is far too small to build it.
4. Do not touch the scanner or any live lane because of the burst results. They are unrelated populations (the scanner names that also burst fade worse).
