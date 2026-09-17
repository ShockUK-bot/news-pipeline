# Current state, 2026-09-17 (written 16:40 CT)

Supersedes the 16:15 version of this file and `claude_current-state-2026-09-16.md`.

## Version and health

- Repo: `v0.16.0` on `main`, tree matches the tag, pushed with tags.
- Deployed this evening: v0.15.4 (C12 realistic entry scoring, `c12-burst` restarted 16:16 CT) and v0.16.0 (scanner sizing 1.0x with a 30 percent lane notional cap, `a3-risk` restarted; nightly `scanner-counterfactual` timer at 15:05 CT, first run backfilled 17 trades; migration 017 applied).
- All services active, `risk` and `burst` heartbeats fresh after the restarts. A4 premarket back to under five minutes on b10064.

## What changed in trading

From the 2026-09-18 open, scanner entries size to about twice their previous notional (about 29,000 on a large cap with a 1 percent stop, 290 at risk; up to 500 at risk on wider stops). The admitted set is unchanged (see the v0.16.0 notes on the viability rule). Nothing else in trading changed; the burst work remains research only.

## Positions

- RIOT (7) gapped up again (21.88), the 20.25 stop did not fire, third day past its exit verdict.
- INVX (49) re-entered by C11 at 29.89 the day after its 29.71 stop out; last 29.17.
- FRMI (33) open, 4.95.
- Scanner today: GNRC short +353.60, CRWV short +190.94, SMCI long +247.56. Best scanner day on record. Lane to date: 17 trades, +1,378, 11 winners.

## Checks for 2026-09-18

1. 08:35 CT: RIOT and INVX. Scanner entries from 08:50: confirm `journal.decisions` RISK SIZE payloads show `risk_budget` about 490 and notional near 29,000 on the first scanner fill; no unexpected `SIZE_CLIPPED` vetoes on scanner items.
2. From 09:10 CT: `burst_report.py --days 1` prints the "Realistic entry" table with `detail.realistic` rows; the go live bar is judged on that table from now.
3. 15:05 CT: `scanner-counterfactual` runs (journal shows the day's trades and the totals table); `scanner_cf` heartbeat OK.
4. 15:50 CT: BURST session 3 read, realistic column.

## Open items

1. Burst: keep measuring (200 rows, +0.15 percent after cost at the realistic entry). Not a lane yet.
2. Scanner: decision on "no scale out" at 30 trades from the journaled counterfactuals (+425 on 17 so far, but it doubles the loss on the one trade that reversed after target).
3. Thesis lane: review exit at next open (RIOT), re-entry cooling off (INVX).
4. Scanner sector cluster rule (three AI semis capped together today); dashboard cosmetics; model file tidy; Qwen3.8-Flash-Next evaluation; scanner freshness `or 60` bug status.

## Files for the design chat

`claude_burst-review-2026-09-17.md`, `claude_scanner-review-2026-09-17.md`, `claude_patch-notes-v0_15_4.md`, `claude_patch-notes-v0_16_0.md`, and this file.
