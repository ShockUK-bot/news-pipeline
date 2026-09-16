# "1% gain" strategy: design study, 2026-09-15

Goal as stated by the operator: a priority lane, like the scanner, that snipes a moving stock long or short for a quick 1 percent. Paper account, so no shadow phase was requested. Before designing a lane I tested whether the data already flowing through the system can find such trades. Read only; scripts in `ops/research/`.

## Summary

With the data the system has today, no version of a 1 percent snipe shows positive expectancy after costs. Four independent tests, 3,881 detection paths and about 2 million 1 minute bars, all land between −0.04 and −0.19 percent per trade. The reasons are structural, not tuning: the names the scanner finds carry a median spread of 29 bps, the liquid names revert after a burst more often than they continue, and the winning moves complete within one to two minutes of detection while the system's detection cadence is 60 seconds on a daily movers list. The pipeline itself is fast enough (detection to fill 0.8 to 1.9 minutes on real trades); the signal is what is missing. Recommendation: do not build an order path yet; build the data feed and the detector, journal would be trades for two weeks, then decide. Details and the exact build below.

## What was tested

### 1. First hit after detection (scanner and gate rows, 30 days)

For every scanner detection with a 3 percent or larger move (2,312) and every gate verdict (1,569), 1 minute bars from the detection minute: which comes first, +1 percent favourable or −1 percent adverse, within 30 and 60 minutes.

| Population | n | +1% first | −1% first | neither (30 min) | median minutes to +1% |
|---|---|---|---|---|---|
| Scanner, long | 1,313 | 38% | 45% | 17% | 2 |
| Scanner, short | 999 | 37% | 44% | 19% | 2 |
| Gate (news), long | 806 | 8% | 7% | 85% | 10 |
| Gate (news), short | 763 | 14% | 16% | 69% | 4 |

Expectancy per trade before costs, 1 percent target and 1 percent stop, 30 minutes: all scanner −0.07 percent. Best sub population, "new extreme 3 to 10 minutes ago on 8x or more relative volume" (n=111): +0.14 percent, or +0.18 with a 0.5 percent stop. First 30 minutes of the session with fresh, high volume detections (n=416): +0.005 percent. After 09:00 CT the same rule is −0.10 percent. Liquid, tight spread names in the sample (spread 10 bps or less, ADV over 1bn, n=73): −0.07 percent.

Spread at detection across scanner rows: p25 8.6 bps, median 28.7, p75 86.5, mean 142. A 1 percent target cannot carry that.

### 2. Burst rules on a liquid universe (83 names with ADV over 1bn, 21 sessions, 1.27 million bars)

Signals from closed bars only, entry at the next bar's open, bracket on subsequent highs and lows, 10 bps round trip cost. Twelve variants of "2 to 3 minute return of 0.5 to 1 percent on 3 to 6x median volume, at a new 30 minute extreme":

| Variant | n | trades/day | EV per trade | target first | stop first |
|---|---|---|---|---|---|
| 2 min ≥0.6%, 4x vol, target 1%, stop 0.5%, 15 min | 281 | 13 | −0.14% | 18% | 51% |
| same, stop 0.7% | 281 | 13 | −0.15% | 19% | 34% |
| same, stop 1.0% | 281 | 13 | −0.16% | 20% | 19% |
| 1 min ≥0.5%, 5x vol | 147 | 7 | −0.10% | 20% | 29% |
| 2 min ≥1.0%, 6x vol (rarer, bigger) | 37 | 2 | −0.19% | 27% | 57% |
| first hour only | 65 | 3 | −0.19% | 28% | 49% |
| target 0.5%, stop 0.35%, 10 min | 281 | 13 | −0.16% | 28% | 60% |
| **fade** the burst (trade against it), 1% / 0.7% | 317 | 15 | −0.04% | 18% | 34% |

Longs and shorts lose alike (−0.13 and −0.16). Per day EV was negative on 16 of 21 sessions. Fading bursts is the only variant near zero (median trade +0.12 percent); with a 2 percent target and 1 percent stop over 60 minutes the fade reaches +0.04 percent, driven by a few large winners (9 percent hit the target, 43 percent the stop).

Splitting the bursts by context: bare bursts (no news, unknown to the scanner) −0.16 percent; bursts in names the scanner had already flagged that day −0.16 percent; news anchored bursts in this universe: 3 in 30 days, too few to measure.

### 3. Enter on A1 escalations (707 regular hours escalations with a direction hint, 30 days)

| Entry | n | 1%/0.7% 15 min | 1%/1% 30 min | 2%/1% 60 min | 0.5%/0.5% 15 min |
|---|---|---|---|---|---|
| A: next bar after the escalation, hinted direction | 707 | −0.11% | −0.07% | −0.04% | −0.11% |
| B: only if a burst confirms within 5 minutes | 59 | −0.10% | −0.10% | +0.12% | −0.15% |
| B, shorts only | 25 | −0.06% | −0.02% | +0.36% | −0.07% |

The escalation itself has no short horizon edge (17 to 25 percent reach the target first, about as many hit the stop, most do nothing). The burst confirmed subset is 59 trades; the one positive cell (2 percent target, shorts, n=25, median trade −0.03 percent) is a handful of winners, not a strategy.

### 4. What the live scanner trades say about latency

Thirteen real scanner entries: detection to A1 verdict 0.0 to 0.8 minutes, detection to fill 0.8 to 1.9 minutes (one 4.7), fill 0 to 0.6 percent from the detection price. The current path can act inside a minute. Median time to +1 percent for winners was 1 to 2 minutes, so the system is not slow by much, but the detector (a 60 second poll of the daily movers list, which measures "moved today", not "moving now") sees the burst only after it has mostly played out.

## Why this fails with today's data

1. **Detection is daily movers on a 60 second poll.** By the time a name is up 8 percent on the day and appears on the list, the impulse is over; what follows is a coin flip with a 29 bps spread on top.
2. **Liquid names revert.** In tight spread names, a 0.6 percent two minute burst is followed by the stop more often than the target on every horizon and stop width tested. Momentum at the 1 to 5 minute scale in large caps is mostly noise.
3. **News names do not move 1 percent in 30 minutes.** 60 to 99 percent of gate rows reach neither level; the ones that do are the same volatile names as above.
4. **Costs.** A 1 percent target with the observed spreads and a 10 bps modelled cost leaves nothing; the best pre cost bucket (+0.14 to +0.18 percent, n=111) is inside the noise after cost.

## What would be needed

New data, in order of value:
1. **Real time trades, quotes and 1 second bars from Alpaca's data websocket** (SIP; the account already has the SIP feed, so this is a code build, not a subscription). This turns detection from "60 seconds after a daily mover appears" into "within seconds of the burst starting", the only regime where a 1 to 2 minute move is catchable. Also gives live spread, so the lane can refuse anything wider than about 5 bps.
2. **A defined intraday universe** (a few hundred liquid names streamed all session) instead of the screener's daily top 50 movers and top 50 actives; bursts in liquid names cannot be seen at all today.
3. Not available on Alpaca and not needed for a first version: order book depth, options flow, tick by tick imbalance.

What is not needed: more news sources (news is not the driver of 1 percent moves at this horizon), a faster pipeline (already about a minute), or a shadow phase for a lane whose signal has not been found yet.

## Recommended build (research first, order path second)

1. **C12 stream service**: subscribe to Alpaca's data websocket for the universe; keep a rolling 1 second and 1 minute bar and quote state per symbol in memory; write nothing but heartbeats and a small `journal.burst_events` table: symbol, detection time, rule, return, volume multiple, spread, and the forward path at 1, 5, 15 and 30 minutes filled in by a sweep (same shape as `gate_counterfactuals`). No order path. Two weeks of data is 20 sessions of every burst in the universe, measured at second resolution.
2. **Detector rules to journal** (all at once, they are cheap): momentum continuation (the rules tested above at 5 to 15 second resolution), burst fade, and news anchored bursts (join with A1 escalations, the one candidate the current data could not size).
3. **Decision rule for going live**: a rule with at least 200 journaled events, EV after a measured cost of at least +0.15 percent per trade and no more than 40 percent of sessions negative. If nothing clears that bar in two weeks, the lane is not built and nothing was lost but the feed, which is useful anyway (it would also give C4 second resolution stops and the scanner a live universe).
4. **Only then** the lane: C12 emits a priority intent straight to A3 (code only sizing, no analyst call, like the scanner lane) with a `snipe_v1` profile (target 1 percent full exit, stop from the measured adverse excursion, time stop 10 to 15 minutes, force flat, no overnight). The exit ladder needs one addition for this: a full exit at target (today it only knows "scale out half" and "flag for review").

Effort: the stream service and journal table are the bulk (about a day); the detector rules are small; the lane after that is a few hours because A3 and C4 already do the rest.

## Files

- Scripts: `ops/research/onepct_firsthit.py`, `onepct_report.py`, `burst_backtest.py`, `burst_context.py`, `escalation_burst_backtest.py` (README there).
- Data used: `journal.scanner_candidates`, `journal.gate_counterfactuals`, `journal.decisions` (TRIAGE), Alpaca 1 minute bars (SIP) for 83 liquid names and every escalated ticker day of the last 30 days.
