# Current state 2026-11-13, evening (supersedes the 09:10 CT file of the same date)

## Version live

`v0.31.6`, deployed 15:29 CT. `a3-risk`, `c3-gate`, `c4-exec`, `c10-scanner` restarted then; everything else untouched. Tree matches the tag.

## What was done today

1. Review challenge of `docs/claude_october-loss-review-2026-11-02.md` and `docs/claude_thu-fri-review-2026-11-07.md` against the live journal. Summary in `docs/claude_patch-notes-v0_31_5.md` section 4.
2. `v0.31.5` (market hours, no restart): scanner replay variants on the live exit ladder (`v27`, `v27_be1.5`, `v27_nobe`) and `asrun`; the nightly job backfills variants added later. Backfill ran at 15:20 CT.
3. Trend day mode back test (510 sessions): the trigger does not select trend days. `docs/claude_patch-notes-v0_31_5.md` section 3.
4. `v0.31.6` (evening): dilution shorts off, scanner start back to 10:00 ET, trend day mode off, news longs to shadow (`LANE_SHADOW`). `docs/claude_patch-notes-v0_31_6.md`.
5. Model review (no change made): triage Qwen3.5 9B, analyst Qwen3.8 27B, heavy Qwen3.5 122B are the newest open Qwen models for each slot. Unsloth republished the 27B files on 11-11 (Dynamic V3.1). Qwen 4 has no open weights yet.

## What places orders now

Only the scanner, long and short, from 10:00 ET, normal caps (2 concurrent, 2 per scan, 6 per hour, gross short 0.30). In shadow or off: news longs (`LANE_SHADOW`, other sources `LANE_SOURCE`), all news shorts including dilution, thesis lane (paused), trend day mode (off), early window 09:33 to 10:00, earnings continuation, gap fade, fade, drift short.

## Today's trading

Trend day flag set at 09:15 CT (QQQ -2.03 percent). Three scanner shorts opened within two minutes: AMD +3, SNDK -421, IREN -366, total -784. The mode is now off.

## Open items

- Monday 2026-11-16: confirm the first `LANE_SHADOW` row and that nothing is emitted before 10:00 ET.
- Scanner exit ladder: under `v27` four trades supply +5,923 and the other 47 sum to -2,160. The breakeven ratchet is not the problem. No change proposed yet.
- Offered, not approved: a direction accuracy bench for the analyst (replay past news with known outcomes) to test the republished 27B file and Qwen 4 27B when it ships; exit cancel race fix; earnings calendar duplicate dates (NBIS, CRWV); shadow sizing (1 R at about 1,160 dollars); `CLAUDE.md` version text refresh (still says v0.26.4); removing unused July model files in `/opt/models` (about 28 GB).
- `backup_offsite` DEGRADED (Google Drive token), unchanged.
- Position 27 (SMTC, 2026-08-26) has no replay rows.

## Rollback

`git revert` the v0.31.6 commit, then with `c7-watchdog.timer` stopped: `sudo -n systemctl restart a3-risk c3-gate c4-exec c10-scanner`. Evening only.
