# Current state, 2026-09-18 (written 16:20 CT)

Supersedes `claude_current-state-2026-09-17.md`.

## Version and health

- `v0.23.0` on `main`, tree matches the tag, pushed. Releases today: v0.22.1 (morning briefing HTML fix, 22:00 CT last night), v0.23.0 (scanner funnel counterfactuals, this afternoon). All long running services untouched today.
- Emails: since v0.22.0 two a day in HTML (06:35 morning briefing, 21:20 evening digest) plus Saturday's A9 review; watchdog alerts damped. Today's morning briefing was the first real HTML one.

## Today's trading

- Scanner: MSTR long +243.98 (08:55 to 09:27), HOOD long +120.68 realised on the target half; the runner half (127 shares from 116.37) was promoted to the swing profile at 11:46 after the guard confirmed the regulatory news as the driver, and the overnight rule held it at 14:45 and 14:55 at about +1.6R with the trail at 118.59. Scanner lane to date: 19 closed trades, about +1,740.
- COIN: capped by concurrency at 09:17 (would have made +0.89R with the move), then proposed as a short by the analyst at 09:28 and vetoed on structure (the short would have lost −0.71R, a long +0.58R). AVGO size clipped at 08:53 (stop 0.69 percent of price, under the 0.83 percent viability threshold; would have lost −0.75R).
- RIOT sold at the open by the v0.16.1 open exit; INVX open. Check the evening digest for the final numbers.
- Burst session 3, first day on the honest column: the fade is negative at a realistic entry (short up bursts −0.24 percent after cost, n=27; buy down bursts −0.23 percent, n=18) where the print column said +0.08. Recommendation stands: expect to retire C12 after one more week.

## Built today

- v0.23.0: every untraded scanner candidate replayed both ways nightly into `journal.scanner_funnel_cf`; A9 rules for the concurrency cap and the analyst's shorts on up moves. Backfill of 57 candidates says the caps and vetoes are saving money so far and the analyst short question is a 1.1R gap on a 2R bar (24 cases). `docs/claude_patch-notes-v0_23_0.md`.

## Checks for 2026-09-19 (Saturday) and Monday

1. Saturday 09:00 CT: A9 review email in HTML; expect no proposal, the watch list now including the two funnel rules with their counts.
2. Monday 06:35 CT: morning briefing; HOOD runner still open under `short_term_v1` with the 118.59 trail; the evening digest at 21:20.
3. A11 15:20 CT Monday: the funnel section of the log prints the by outcome table (cosmetic check).

## Open items

1. Burst: retire decision after next week's data.
2. Scanner: no scale out at 30 trades (19 now); concurrency cap and analyst direction via A9 when their bars are met.
3. Operator: the two rollback model files are still on disk (`rm` line from 09-17).
4. Not built: A10 (never defined), C9 replay spec.

## Files for the design chat

`claude_patch-notes-v0_22_1.md`, `claude_patch-notes-v0_23_0.md`, and this file.
