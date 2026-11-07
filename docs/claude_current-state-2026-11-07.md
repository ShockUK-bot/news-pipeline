# Current state, 2026-11-07 (Saturday, written 14:45 CT)

Supersedes `claude_current-state-2026-11-02.md`.

## Live

- `v0.31.0` deployed 14:38 CT (config version e8e16eb): `c3-gate`, `a3-risk`, `c10-scanner` restarted with the market closed and the watchdog paused; all active, heartbeats fresh, no errors. `c4-exec` and `a2-analyst` still run v0.30.0 code (nothing in v0.30.1 to v0.31.0 touches them). Rollback: `git reset --hard v0.30.2` and restart the same three; `v0.26.4` to undo the whole week.
- Migration 023 applied 14:20 CT: `journal.scanner_candidates.status` now allows SHADOW. The catalyst shadow lanes (v0.28.0) start journaling Monday 2026-11-09 at 09:45 ET; they wrote nothing from 11-03 to 11-06 because of the constraint (my miss).
- Trend day mode (v0.31.0) is live and armed: from 10:00 ET the scanner checks QQQ and SPY; down 2 percent and below the 09:45 price sets `journal.control.trend_day` for the session; news shorts on, no new longs, scanner caps and short budgets raised. Nothing changes on other days.
- Week of 11-02: realized +446 after Monday's -626 (MRVL stop); trades RIOT +108 (trail), PODD +46, BE +87, TSLA +149 and AVGO -103 (both scratched by the breakeven ratchet on Thursday's trend day). No open positions. Equity 96,986.
- A9 2026-11-07: proposal 3 (scanner from 09:33 ET, +5.6 R over 37 early window candidates) is PROPOSED and waits for Ian. Proposal 2 still open. Proposal 1 approved 11-02, evaluated next Saturday.
- Reviews: `claude_thu-fri-review-2026-11-07.md` (why Thursday and Friday were not traded, single name and index back tests, the trend day design).

## Open items

1. Done 11-07 15:00 CT: proposal 3 APPROVED and applied (v0.31.1, scanner from 09:33 ET, c10-scanner restarted), proposal 2 REJECTED. A9 evaluates proposals 1 and 3 on 11-14.
2. Breakeven ratchet: v0.30.2 journals `be1.5` and `nobe` variants nightly from 11-09; decide after two weeks. Mechanics bug to fix: the first bar after a fill can count as progress (TSLA ratchet moved six seconds after entry).
3. Normal day gross short budget: one 0.30 scanner position still fills the 0.30 gross short cap outside trend days; proposed 0.15 per position and 0.60 gross (config, needs a yes).
4. Cancel race fix and c4 startup retry (from the October review), still to build.
5. GlobeNewswire 403, Google Drive token, Qwen4 bench when weights land, llama.cpp b12050 was slower (no swap).
6. A9 rule to score trend day sessions separately; digest line when the flag sets.
