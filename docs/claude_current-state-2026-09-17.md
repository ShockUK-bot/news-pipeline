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

## Evening update (17:00 CT): v0.16.1 and v0.17.0

- **v0.16.1** (c4-exec and c6-dashboard restarted 16:45 CT): dead and review-exit theses are sold by C4 at 09:35 ET the next session (RIOT is armed by tonight's 21:15 CT C11 run and sells tomorrow at the open); thesis re-entry cooling off of 5 days after a forced exit (INVX); Gate Lab extended hours scoreboard direction adjusted, fade shadow rows in their own panel; BURST tab shows the realistic entry columns. `docs/claude_patch-notes-v0_16_1.md`.
- **v0.17.0, A11 built** (`a11-metrics.timer` 15:20 CT weekdays, first run 16:54 CT backfilled everything). `docs/claude_patch-notes-v0_17_0.md`. The standalone `scanner-counterfactual.timer` is disabled (A11 runs it).
- First A11 readings worth acting on: A12 HOLD verdicts are 48 shakeouts against 16 saves (it holds losers); EXIT verdicts are 3 saves to 1 shakeout on a sample of 10. Exit efficiency this week 0.26; stop exits −0.56, trail exits +0.57.

## Checks for 2026-09-18 (additions)

5. 08:35 CT: C4 log shows `exit at open` for RIOT (layer REVIEW) and the position closed; INVX untouched by C11 (already held).
6. 15:20 CT: `a11-metrics` runs (journal shows the report, `metrics` heartbeat OK); the DAY rollup for 09-18 exists.

## Open items (replaces the earlier list)

1. Burst: keep measuring on the realistic column (200 rows, +0.15 percent after cost).
2. Scanner: "no scale out" decision at 30 trades, now journaled nightly.
3. A12 guard: the HOLD verdicts are wrong more often than right by the A11 classification; review the guard prompt and the hold threshold before any auto execution decision.
4. Not done, needs the operator: model file tidy (list in the session summary), Qwen3.8-Flash-Next download for the heavy slot evaluation, a sector data source before a sector cluster rule.
5. Next agents: A9 (weekend review and proposals) now has rollups to read; A10 was never defined; C9 replay spec.

## Files for the design chat (final for today)

`claude_burst-review-2026-09-17.md`, `claude_scanner-review-2026-09-17.md`, `claude_patch-notes-v0_15_4.md`, `claude_patch-notes-v0_16_0.md`, `claude_patch-notes-v0_16_1.md`, `claude_patch-notes-v0_17_0.md`, and this file.

## Late update (17:15 CT): v0.17.1 and v0.18.0

- **v0.17.1**: A11 guard classification refined; a HOLD on a position already +1R is the ladder's give back, not a shakeout. Reclassified: HOLD 16 saves, 27 shakeouts (12 FRMI), 104 neutral; EXIT 3 saves, 1 shakeout, 6 neutral. Read: the guard is fine, no prompt change, auto execution stays off. `docs/claude_patch-notes-v0_17_1.md`.
- **v0.18.0, A9 built**: Saturday 09:00 CT weekend review with six evidence rules, proposals journaled and emailed, operator approval by telling Claude Code, evaluation the following Saturday. First run: no proposal, six watch items with their sample counts. `docs/claude_patch-notes-v0_18_0.md`.
- Model file tidy: still to run by the operator (the `rm` line in the session summary). `/opt/models` unchanged at 17:15 CT.

## Open items (final for today)

1. Burst: realistic scoring starts 09-18; A9 watches the 200 row bar.
2. Scanner: no scale out decision at 30 trades; A9 proposes it when the bar is met.
3. Sector data source (EDGAR SIC codes are a candidate: the pipeline already has a CIK map) so the sector heat clip and a scanner cluster rule can exist.
4. Qwen3.8-Flash-Next download for the heavy slot evaluation.
5. Not built: A10 (never defined), C9 replay spec. A12 auto execution waits for a bigger EXIT sample (10 so far).

## Files for the design chat (final for today)

`claude_burst-review-2026-09-17.md`, `claude_scanner-review-2026-09-17.md`, patch notes v0.15.4, v0.16.0, v0.16.1, v0.17.0, v0.17.1, v0.18.0, and this file.

## Late update (17:30 CT): v0.19.0 sector data source

- `journal.sectors` from SEC SIC codes, 923 of 1,024 recent tickers mapped, nightly `sector-map.timer` 04:40 CT. A3's sector heat clip is live (1.5 percent of capital per sector, stop based); A2 gets `sector`. `a3-risk` and `a2-analyst` restarted 17:22 CT. `docs/claude_patch-notes-v0_19_0.md`.
- Check for 09-18: the first RISK SIZE decision should carry `sector` in its numbers and no `SECTOR_UNKNOWN` flag for a mapped name; `sectors` heartbeat OK after 04:40 CT.
- Next build candidates: scanner sector cluster rule (now possible), then A12 auto execution once the guard EXIT sample passes 20.
