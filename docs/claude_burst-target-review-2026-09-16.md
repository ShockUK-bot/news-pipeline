# BURST target review and draft for session 2 (2026-09-16, evening)

Written from the afternoon analysis of session 1 of the C12 burst stream. Companion to `claude_1pct-gain-design-2026-09-15.md` (the backtests) and `claude_patch-notes-v0_15_3.md` (the bracket sweep).

## 1. Why 1 percent was the wrong target

Session 1 produced 158 detections across 148 liquid names (spreads averaging 9 bps), each scored both with the burst (momentum) and against it (fade), 30 minute horizon, entry at the detection price. The original bracket was a 1 percent target against a 0.7 percent stop. Caveat: FOMC decision day, 73 of the 158 detections came in the 13:00 CT hour.

How far the price actually travelled in the trade's favour within 30 minutes (153 scored detections):

| Population | Reached +0.3% | +0.5% | +0.7% | +1.0% | Adverse stayed under 0.5% | Median move at 5 / 15 min |
|---|---|---|---|---|---|---|
| Fade an up burst (short) | 100% | **95%** | 76% | 54% | 66% | +0.28% / +0.45% |
| Fade a down burst (buy) | 97% | 78% | 59% | 43% | 52% | +0.22% / +0.28% |
| Chase an up burst (long) | 49% | 34% | 24% | 16% | 5% | −0.28% / −0.45% |
| Chase a down burst (short) | 61% | 48% | 35% | 27% | 22% | −0.22% / −0.28% |

Read:

- The move that is reliably there is **0.3 to 0.5 percent over 5 to 15 minutes, taken against the burst**. Half of the fades never reach 1 percent, so a 1 percent target leaves most of the winners open until they decay or hit the stop.
- Momentum chasing has no target that works. Only 1 in 20 chased up bursts kept its adverse excursion under 0.5 percent; the median chase is under water at 5 minutes and further under water at 15.
- With the 1.0/0.7 bracket the fade still came out ahead (before 13:00: up burst fade +0.42 percent after cost, n=54; down burst fade +0.18 percent, n=19), but that is the bracket hiding the edge, not showing it.

Two standing caveats when reading any fade number: the entry is assumed at the burst's last trade (a real short sells into the burst at the bid and needs borrow, ETB only), and the fade rows are exact mirrors of the momentum rows by construction, so a fade "win" is a momentum "loss" and nothing more.

## 2. What is now measured instead of guessed (v0.15.3)

From session 2 every event is scored against five brackets at once, stored in `detail.brackets`:

| Bracket (target / stop) | Why it is in the sweep |
|---|---|
| 0.5 / 0.5 | The candidate: the move that 95 percent of up burst fades reached |
| 0.7 / 0.5 | Slightly greedier target, same stop |
| 1.0 / 0.7 | The original, for continuity with session 1 |
| 1.0 / 1.0 | Symmetric wide bracket, the design doc's baseline |
| 0.5 / 0.35 | Tight bracket, tests whether a small stop survives the noise |

`ops/tools/burst_report.py --days N` prints the sweep with expected value per trade after cost. Three sessions give about 200 scored rows per rule (fewer on quiet days; session 1 was FOMC inflated, a normal day is 30 to 50 up bursts).

## 3. Draft for session 2 (2026-09-17): nothing changes in trading

The account stays as it is. Nothing in the burst work trades; the sweep decides the target and the rule.

Checks in order:

1. 06:00 CT, A4 premarket back on b10064: p50 per call near 141 s (was 248 s on b10970).
2. 08:35 CT: RIOT (position 7, 34 shares, last 20.35) has its stop tightened to 20.25 by tonight's C11 run (third consecutive A6 exit verdict). It sits 0.5 percent above the stop, so it most likely exits at the open. FRMI (position 33) untouched.
3. 08:35 CT: C11 planned a **re-entry in INVX** (thesis th-2026-003, 62 shares at limit 29.92) the same day INVX was stopped out at 29.71. Watch whether it fills, and flag for design: a thesis re-entry the same day as a stop out has no cooling off rule.
4. From 09:10 CT: first scored rows carry `detail.brackets` with five keys; `burst_report.py --days 1` prints the sweep. If the keys are missing, the v0.15.3 write path is wrong and the day's rows are only scored on the old bracket.
5. 15:50 CT: session 2 scoreboard. The question is whether the up burst fade holds on an ordinary day, and which bracket wins. Read the sweep by rule and direction, before 13:00 and after separately for one more day in case the Fed hangover matters.
6. Go live bar, unchanged: 200 scored rows for the rule, at least +0.15 percent per trade after cost, no more than 40 percent of sessions negative.

## 4. If the fade holds: what v0.16 would be

Not for tomorrow. Recorded so the design chat has the shape.

- C12 emits a priority intent straight to A3 (code only sizing, no analyst call, like the scanner lane). Up burst fades are shorts, so ETB only; down burst fades are longs.
- Profile: 0.5 percent target with a full exit (the exit ladder today only knows "scale out half" and "flag for review", so one addition), 0.5 percent stop, 10 minute time stop, force flat, no overnight.
- Sizing: with a 0.5 percent stop the 15 percent notional cap (15k) binds before the risk cap, so about 15k per trade and 75 dollars at risk. At +0.3 percent net and 30 trades a day that is roughly +1,350 a day, using the account through the session instead of the 1 to 2 percent it uses now. If the edge is not there, the loss per trade is small by construction.
- Do not raise `risk_per_trade` or the daily cap for the existing lanes: the live news long lane is −1,254 over 18 trades and the shadow data says the vetoed longs would have lost. Making those lanes bigger scales a loser.
