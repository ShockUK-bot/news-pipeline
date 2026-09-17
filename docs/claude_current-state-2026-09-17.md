# Current state, 2026-09-17 (written 16:15 CT)

Supersedes `claude_current-state-2026-09-16.md`.

## Version and health

- Repo: `v0.15.3` on `main`, tree matches the tag, pushed. All services active, heartbeats fresh at the 09-16 evening check. No restarts today.
- v0.15.3 verified: all 73 scored burst rows today carry `detail.brackets`; the sweep prints.
- A4 premarket on the restored heavy build (b10064): 06:00 to 06:04:49, back to normal.

## Positions

- RIOT (7) gapped up again (21.88) and the 20.25 tightened stop did not fire; still open, third day past its exit verdict. Design item: review exits should sell at the next open.
- INVX (49) re-entered by C11 at 29.89 at 08:45, the day after being stopped at 29.71; last 29.17. Design item: cooling off after a stop out.
- FRMI (33) open, 4.95.
- Scanner today: GNRC short +353.60, CRWV short +190.94, SMCI long +247.56. Best scanner day on record.

## Reviews written today

- `docs/claude_burst-review-2026-09-17.md`: session 2, 0.5 / 0.5 is the best bracket, but 31 of 45 target hits are inside the first minute and the realistic entry return on an ordinary day is +0.06 percent. Not a lane. Recommends v0.15.4 (score from a 30 second delayed entry with half spread, exclude repeat bursts, cap fade size) and continued measurement.
- `docs/claude_scanner-review-2026-09-17.md`: 17 trades, +1,378, 11 winners, shorts 4 of 4. The binding limit is the 15 percent notional cap, not risk. Recommends scanner risk multiplier 1.0 plus a lane specific 25 percent notional cap, and a nightly journaled counterfactual per scanner trade. Ladder, gate, caps and timing unchanged.

## Open items

1. Decision on scanner sizing (review item 1) and the counterfactual job (item 2).
2. v0.15.4 burst scoring honesty (burst review item 1).
3. Thesis lane: review exit at next open (RIOT), re-entry cooling off (INVX).
4. Dashboard cosmetics; model file tidy; Qwen3.8-Flash-Next evaluation; scanner freshness bug status check (`rules.py` `or 60`, v0.14.6 scope) before any scoring tune.

## Files for the design chat

`claude_burst-review-2026-09-17.md`, `claude_scanner-review-2026-09-17.md`, and this file.
