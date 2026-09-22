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
