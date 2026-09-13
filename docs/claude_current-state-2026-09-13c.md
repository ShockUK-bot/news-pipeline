# Current state, 2026-09-13 (end of session, 15:00 CT)

Definitive file for today. Supersedes `claude_current-state-2026-09-13.md` and `claude_current-state-2026-09-13b.md`; those stay for the record.

## Version and health

- Repo: tag `v0.14.7` on `main`, working tree identical to the tag, `main` and `origin/main` in sync, all tags on GitHub. Twelve commits today on top of v0.14.5.
- Running code: `c10-scanner` and `c4-exec` restarted on v0.14.6 at 14:55 CT (v0.14.7 changed tests only, so they are current). Every other service unchanged since its last restart (08-27, 09-03 or 09-11).
- All long running agents and components active; all scheduled units reported success on their last run. Heartbeats for `risk`, `exec`, `analyst`, `gate`, `triage`, `deadman`, `watchdog` all OK and fresh. `scanner` is a few minutes old, which is normal off hours (C10 writes it only while scanning).
- Account: Alpaca paper, equity about 97,400.

## What was done today

1. Claude Code set up on the box; `CLAUDE.md` service list corrected to what is actually installed; `docs/` created; obsolete `llama-analyst` unit file removed.
2. A3 risk heartbeat: healthy now. Cause of the 09-11 stale row was the v0.14.4 changeset tagged but never written to the working tree (fixed by the 09-11 re sync). Resolved note plus two gotchas in `CLAUDE.md` (`journal.health` has no history; `ingestion:*` rows are connection events, not liveness).
3. Repo was on a detached HEAD from the re sync. `main` moved to the working commit and checked out; `git push` and `git checkout` allowed for Claude Code; rule added to `CLAUDE.md`: work on `main`, never check out a tag, confirm `git diff <tag> --stat` is empty before restarting.
4. Scanner profitability review (`docs/claude_scanner-review-2026-09-13.md`): six scanner longs -377.74 and four news trades -345.55 in the week of 09-04; five of six scanner entries only cleared the 0.60 floor because of the v0.14.5 liquidity term; the 2.0x large cap volume bar admitted one trade (a loss) and still rejects the MSTR style cases it was built for; freshness scoring bug found.
5. Counterfactual and slippage review (`docs/claude_scanner-counterfactual-2026-09-13.md`): no bars are journaled, so the scalp ladder was replayed on Alpaca 1 minute bars (validated against the six real trades). Consistent finding across longs and shadow shorts: the 08:50 to 09:00 CT entry window, not the stop width. Stop slippage over 12 exits since 08-15 averages +0.06R; SPCX (0.62R) and CRWD (0.54R) are the outliers, both large cap scanner longs exited in the first 40 minutes.
6. **v0.14.6 built, deployed 14:55 CT** (`docs/claude_patch-notes-v0_14_6.md`): freshness credit fix in C10; provisional pre close invalidation pass at 15:55 ET in C4 (code default, no knob); `trigger_price`, `slip_px`, `slip_r` journaled on EXIT and SCALE_OUT events; `ops/tools/scalp_replay.py` with a usage note in `docs/README.md`. 18 new tests.
7. **v0.14.7** (`docs/claude_patch-notes-v0_14_7.md`): three stale unit tests fixed, suite 787 passed and 1 skipped. Tests only, no restart.

## Open items

1. **Monday 09-14 checks** (first session on v0.14.6):
   - First EXIT or SCALE_OUT `position_event` should carry `trigger_price`, `slip_px`, `slip_r`.
   - Around 14:55 CT `journalctl -u c4-exec` should show the pre close pass; a `pre-close invalidation` line and an `INVALIDATION_FIRED` event with `provisional: true` appear only if a session predicate fires.
   - The `risk`, `dedup`, `chat` heartbeats should stay fresh all session (first full day since the 09-11 re sync).
2. **Scanner entry timing.** Both reviews point at the first ten minutes of the scanner session rather than the score floor or volume bar. Candidate changes: later `session_start_et`, or a wider first hour stop. Evidence wanted before any config change: 30 or more scanner longs, forward returns on the SCORE_FLOOR and REL_VOLUME rejected pool (the replay tool makes this cheap to script), the shadow short outcomes, and stop fill quality now that slippage is journaled.
3. **Large cap volume tier** has not caught the case it was built for (MSTR rejected at 1.3x to 1.9x three more times). Leave until the evidence above exists.
4. `tests/unit/test_cik_map.py` still carries a module level asyncio mark on synchronous tests (8 warnings). Harmless; tidy when next in that file.
5. Design questions from today not yet decided: whether the news lane invalidation exit that fails at 16:01 should try again at the next open instead of waiting for the 14:45 overnight rule (partly addressed by the 15:55 pass); whether to add a config knob for the pre close time in a later release.

## Next steps

- After Monday's session, run the Monday checks above and note results in the next current state file.
- No release planned. The next feature release should start from the evidence items in open item 2, not from a config change.

## Files for the design chat (today)

`claude_patch-notes-v0_14_6.md`, `claude_patch-notes-v0_14_7.md`, `claude_scanner-review-2026-09-13.md`, `claude_scanner-counterfactual-2026-09-13.md`, and this file. The earlier `claude_current-state-2026-09-13.md` and `-13b.md` are superseded by this one.
