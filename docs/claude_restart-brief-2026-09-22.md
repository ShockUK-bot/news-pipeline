# Restart brief (written 2026-09-22 for the October absence)

Keep this file. It is the one document that lets a fresh Claude Code session, or a fresh design chat, pick the system up with no memory of the sessions that built it.

## What survives what

- **The GitHub repo** (`ShockUK-bot/news-pipeline`, private) holds the code, every config, every migration, every patch note and every current state file under `docs/`. If the Spark dies, the whole system can be rebuilt from it; only the journal (trades, decisions, measurements) would be lost.
- **The journal** (Postgres `trading` on the Spark) is the only copy of the history. Nightly dumps go to `~/pipeline-backups` (14 days kept) on the same disk. Until an off box copy exists, the daily and Saturday emails are the off box record of what happened: keep them.
- **This chat's memory** is not needed. Everything a future session needs is in `CLAUDE.md`, the newest `docs/claude_current-state-*.md`, and the patch notes.

## Prompt to restart a Claude Code session on the Spark

Paste this as the first message:

> Read CLAUDE.md, then `git describe --tags`, `git status --short`, and the newest `docs/claude_current-state-*.md` and `docs/claude_restart-brief-2026-09-22.md`. Report the version live, whether the tree matches the tag, which services and timers are active and which heartbeats are stale, the last five Saturday A9 reviews (journal.decisions agent A9) and any proposals still PROPOSED, and the lane records for the last 30 days (journal.positions by origin and side). Then propose the month review in `docs/claude_month-review-YYYY-MM.md` and wait for my go.

## Prompt to restart the design chat

Upload `CLAUDE.md`, this file, the newest current state file, and the patch notes from v0.15.0 onward, then say:

> This is a locally hosted multi agent paper trading pipeline. The uploaded files are its current state. Summarise what is built, what is measured, what is in shadow, and what the open decisions are, then help me design the next release from the evidence in the latest month review.

## Where things stood on 2026-09-22 (v0.25.0)

- Every agent in the design is built except A10 (never defined) and the C9 replay spec. The system measures itself nightly (A11), proposes weekly (A9, Saturday 09:00 CT email), and the operator approves by telling Claude Code "approve proposal N".
- Live lanes: scanner (the one that works: 19 closed trades, about +1,740), news longs and shorts (losing), thesis (losing, small). Guard auto executes high urgency exits on watch list hits or corrections. Sector clip 1.5 percent net, scanner exempt.
- Shadows started 2026-09-22, journal only, A9 rules attached: bad news drift short (`rule=drift_short`), widened bullish fade (`rule=fade`), scanner early window 09:33 to 09:50 ET (`CAPPED / EARLY_WINDOW` rows replayed by A11).
- Retired: the C12 burst stream (no edge at a realistic entry). Its trade book and detector are kept in `src/c12_burst/` for a scanner fast lane; plan in `docs/claude_c12-retirement-and-reuse-2026-09-22.md`.
- Emails: 06:35 CT morning briefing, 21:20 CT evening digest, Saturday A9 review, all HTML; watchdog alerts only for confirmed problems.

## Next steps as of 2026-09-22, in order

1. Read the Saturday A9 emails. Any proposal marked PROPOSED waits for "approve proposal N"; A9 never applies anything itself.
2. When the scanner early window proposal arrives (20 early candidates summing to +3R, half winners), build the scanner fast lane from the C12 code.
3. Scanner no scale out at 30 closed trades (A9 proposes it from the journaled variants).
4. Drift short and fade lanes when their bars are met (60 would trades, +0.4 percent after cost, median above zero, 55 percent winners, no more than 40 percent losing sessions).
5. Design items not yet built: the analyst's don't chase rule on rated initiations, a sector cluster rule switch to veto if A11 shows clustered scanner entries lose, A12 tighten stop auto execution once its sample exists.

## The month review on return

Ask Claude Code for `docs/claude_month-review-2026-10.md` covering: lane P&L by origin and side, exit efficiency by layer, guard save rate, the three shadows against their bars, scanner funnel counterfactuals (concurrency cap, analyst shorts on up moves, early window), proposals raised and their status, watchdog alerts and any service restarts in the journal, and the evening digest "needs you" items that were never acted on.
