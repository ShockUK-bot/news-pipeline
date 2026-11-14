# Two year test of the scanner entry, follow and fade (2026-11-13)

Read only. Scripts: `ops/tools/research/mover_bt.py` (builds the events), `mover_an.py` (tables), `trend_bt.py` (the trend day test in `docs/claude_patch-notes-v0_31_5.md`). Nothing live reads them.

## Method

- 555 names the scanner has seen with 25 million dollars a day or more; 5 minute Alpaca bars, 2024-11 to 2026-11-13, 513 sessions.
- Signal, first hit per name per day, bar ending 09:45 to 15:15 ET. A "move": 4 percent or more against the previous close, price 5 or more, 20 day dollar volume 25 million or more (62,848 events, 122 a day). B "scanner": A plus relative volume 3x (2x for names over 1 billion a day) and the day's extreme set in the last 60 minutes (7,793 events, 15 a day). B is the closest to what the live scanner emits.
- Entry at the open of the next 5 minute bar. Both sides simulated. Exits: the live ladder (2 ATR stop, breakeven at 0.75 R, 3 ATR trail from 1 R, flat 15:50 ET) and a plain 2 ATR stop held to 15:50.
- Year 1 is before 2025-11-13, year 2 after. t is on daily means (one vote per day).

## Result: mean R per trade

| Signal | Trade | Cost | Year 1 | Year 2 |
|---|---|---|---|---|
| B scanner | follow, ladder | none | +0.001 | +0.075 |
| B scanner | follow, ladder | 4 bp | -0.024 | +0.052 |
| B scanner | follow, ladder | 10 bp | -0.062 | +0.019 |
| B scanner | fade, ladder | none | +0.064 | -0.074 |
| B scanner | fade, ladder | 4 bp | +0.039 | -0.096 |
| B scanner | fade, ladder | 10 bp | +0.002 | -0.130 |
| A move | follow, ladder | none | -0.014 | +0.013 |
| A move | follow, ladder | 10 bp | -0.098 | -0.065 |
| A move | fade, ladder | none | +0.076 | -0.005 |
| A move | fade, ladder | 10 bp | -0.008 | -0.083 |

Cost is the round trip in basis points of price. The median R unit is 1.5 to 1.9 percent of price, so 10 bp costs about 0.06 R a trade.

- Before costs the entry is worth about zero in either direction. Follow and fade swap signs between the two years; neither holds in both.
- No split rescues it (index down, flat or up at the signal; time of day; move size; with or against the index; up or down movers). The best looking cell, fading down movers when the index is down 1 percent or more, was +0.18 R in year 1 and -0.05 R in year 2.
- The ladder wins about 30 percent of the time in both directions.
- The live scanner's +484 over 54 trades is consistent with this: no edge, plus luck.

## Limits

Today's liquid names (survivorship), 5 minute bars against the live 1 minute exits, no earnings blackout, no spread or borrow filter, ATR from 14 bar ranges. None of these plausibly turns zero into an edge.

## Conclusion

The scanner entry (a liquid name up or down 4 percent intraday) has no edge to follow or to fade with intraday exits. Tuning stops, start times or caps on this entry cannot fix that. The search has to move to a different kind of signal, tested this way before anything is built.
