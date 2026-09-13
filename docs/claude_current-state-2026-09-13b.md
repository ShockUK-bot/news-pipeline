# Current state, 2026-09-13 (second file, end of day)

Supersedes `claude_current-state-2026-09-13.md` for anything it repeats.

## Version

- Repo: tag `v0.14.6` on `main`, pushed to GitHub with tags. `main` and `origin/main` in sync.
- Running services: `c10-scanner` and `c4-exec` restarted on v0.14.6 at 14:55 CT on 2026-09-13. Every other service is unchanged since its last restart (same code paths, untouched by this release).

## What was done today, in order

1. Claude Code setup, service list fix, `docs/` folder, stale `llama-analyst` unit removed (commits `edce282`, `7b84668`, `8148f5b`).
2. A3 risk heartbeat investigation: heartbeat healthy now; cause of the 09-11 stale row was the v0.14.4 changeset tagged but not written to the working tree; resolved note in `CLAUDE.md` (`f969463`).
3. Detached HEAD fixed: `main` moved to the working commit and checked out; `git push` and `git checkout` added to the Claude Code allow list; `CLAUDE.md` rule added: work on `main`, never check out a tag (`7e44aa0`, `8948c79`).
4. `ingestion:*` health rows documented as connection events, not liveness (`ddb6ccd`).
5. Scanner profitability review: `docs/claude_scanner-review-2026-09-13.md` (`ba9d760`). Six scanner longs -377.74, four news trades -345.55; five of six scanner entries only existed because of the v0.14.5 liquidity term; freshness bug found.
6. Counterfactual and slippage review: `docs/claude_scanner-counterfactual-2026-09-13.md` (`0f4815c`). No journaled bars exist; replayed on Alpaca 1 minute bars. Entry timing, not stop width, is the consistent finding. SPCX slippage is not a one off (CRWD 08-28, 0.54R).
7. v0.14.6 built and tagged: freshness fix, pre close invalidation pass at 15:55 ET, slippage on exit events, `ops/tools/scalp_replay.py`. See `docs/claude_patch-notes-v0_14_6.md`.

## Deploy status

DEPLOYED 2026-09-13 14:55 CT (Sunday, market closed) with operator go.
- `c7-watchdog.timer` stopped, `c10-scanner` and `c4-exec` restarted, timer started again.
- Verification: both units `active`; both journals show `config version active` `ea856e3fde9b` (the v0.14.6 commit) and zero errors or tracebacks; `journal.health` rows `scanner`, `exec` and `deadman` OK and refreshed within a minute of the restart.
- Rollback if needed: `git reset --hard v0.14.5` on `main`, then `sudo -n systemctl restart c10-scanner c4-exec`.

## Open items

1. **Three pre existing unit test failures**, unrelated to v0.14.6 and present on the previous commit: `test_cik_map.py::test_end_to_end_stored_with_symbols` (needs `PIPELINE_DSN`), `test_a7_c5.py::test_render_busy_day_with_narrative` and `test_triage_v047.py::test_confidence_required` (stale assertions). Worth a small cleanup release so the suite is green again.
2. **Scanner entry timing.** Both reviews point at the 08:50 to 09:00 CT entry window rather than the score floor or the volume bar. Candidates: later `session_start_et`, or a wider first hour stop. Needs the shadow short and rejected pool evidence described in the counterfactual doc before any config change.
3. **Rejected pool forward returns** (SCORE_FLOOR and REL_VOLUME rows) are still not computed; the replay tool now makes this cheap to script.
4. **First day of the pre close pass** is Monday 09-14 at 14:55 CT if deployed. Check `journalctl -u c4-exec` around that time and any `INVALIDATION_FIRED` events with `provisional: true`.
5. From the earlier file: `patch-notes` and deploy guide for v0.14.5 now live in `docs/`; `.env` symlink does not exist (recipe fixed in `CLAUDE.md`).

## Next steps

- Operator: give the go for the v0.14.6 restart (any time, market closed).
- After Monday's session: confirm `slip_px` appears on the first exit event and the 14:55 pass ran.
- Consider the test cleanup release (open item 1) before the next feature release.
