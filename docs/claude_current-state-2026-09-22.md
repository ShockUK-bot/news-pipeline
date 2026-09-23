# Current state, 2026-09-22 (written 16:05 CT)

Supersedes `claude_current-state-2026-09-18.md`.

- `v0.24.0` on `main`, tree matches the tag, pushed. Three shadow strategies live from tomorrow (bad news drift short, widened bullish fade, scanner early window 09:33 to 09:50 ET), all journal only, A9 rules attached. `docs/claude_patch-notes-v0_24_0.md`. `c3-gate`, `c10-scanner`, `c6-dashboard` restarted after the close.
- SNDK today: scanner bought the top at 09:51 ET after a move made entirely inside the open blackout; −395.67. The early window shadow measures the fix from tomorrow. The news lane discarded the coverage initiation at 06:43 by an A1 rule (design item, not changed).
- HOOD runner from 09-18 still open under the swing profile (check the evening digest).
- Checks for 09-23: early window rows appear as CAPPED / EARLY_WINDOW from 08:33 CT (no emissions before 08:50); the first `rule='drift_short'` and widened `fade` GATE rows; A11 at 15:20 shows CAPPED_EARLY_WINDOW replays; the Gate Lab shadow panel shows both lanes.
- Open: burst retire decision this week; scanner no scale out at 30 trades; A1 treatment of coverage initiations; operator still to delete the two rollback model files.

Files for the design chat: `claude_patch-notes-v0_24_0.md` and this file.

## Late update (16:30 CT)

- v0.24.1/v0.24.2: rated coverage initiations are material at triage (`a1-triage` restarted 15:51 CT). v0.24.1 was tagged with one test failing (a test bug, prompt correct); fixed as v0.24.2.
- v0.25.0: C12 burst stream retired (stopped, disabled, removed from dashboard and watchdog, A9 rule retired; code, config and table kept). Reuse plan for a scanner fast lane in `docs/claude_c12-retirement-and-reuse-2026-09-22.md`, gated on the early window shadow evidence.
- Files for the design chat: `claude_patch-notes-v0_24_1.md`, `claude_patch-notes-v0_25_0.md`, `claude_c12-retirement-and-reuse-2026-09-22.md`, and this file.
- v0.25.1 (16:45 CT): drawdown breaker side aware (a winning short no longer reads as a loss), auto reset at the next session with a 3 trips in 30 days limit, trip and reset emails; `llama-heavy-guard.timer` 08:15 CT stops a heavy slot left running. `c4-exec` restarted. Restart brief for the absence: `docs/claude_restart-brief-2026-09-22.md`. Off box backup destination still to be chosen by the operator.
- v0.25.2 (21:15 CT): off site backups: nightly journal extract to the GitHub branch `journal-extract` (live, first push done), Google Drive dump copy ready pending the operator's rclone authorisation (`docs/claude_backup-offsite-2026-09-22.md`; operator will do it 2026-09-23).
- v0.26.0 (21:40 CT): percent profit lock on the thesis and long term profiles (up 8 percent, trail 5 percent behind the high, applies to FRMI and INVX now) and price confirmed thesis re-entry (reclaim the stop out level; cooling off 1 day). `c4-exec` restarted. `docs/claude_patch-notes-v0_26_0.md`.
- Checks for 2026-09-23: FRMI (+10 percent) should show a TRAIL ratchet with "profit lock" in its first engine passes after 08:30 CT; C11 at 21:15 journals REENTRY_WAIT for any stopped name still below its exit level; the 02:50 Google Drive job reports DEGRADED until authorised; the 03:05 extract pushes.
