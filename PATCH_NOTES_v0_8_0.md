# v0.8.0 — Phase 7: A4 Pre-Market Review + open handoff (2026-07-18)

Implements Phase 7 of `trading-system-baseline` v0.5: the consumer of
`signal.overnight` (accumulating unread since Phase 2) and the overnight →
open trading path. Third release of 2026-07-18, after v0.6.0 (A12) and
v0.7.0 (A7/C5).

## What happens at 07:00 ET every weekday now

1. **Bulk expiry (code, one SQL pass):** overnight messages older than
   `sheet.max_age_hours` (72) are retired and journaled as ONE
   `PREMARKET/EXPIRED_BULK` summary row — this drains the multi-week
   backlog on first start with zero tokens and no journal spam.
2. **Code routes before any model call:** items touching an open position →
   `signal.guard` priority 0 (A12); material items with no ticker →
   `signal.thesis`. Capital protection is not a ranking decision.
3. **The heavy model ranks the rest** — top-K (15) fresh candidates in ONE
   grammar-constrained call → ActionSheet (`open_candidate | thesis |
   ignore`, rank, one-line rationale each + a 2-4 sentence overnight
   summary). Shared SlotManager from v0.7.0: heavy started on demand at
   07:00 and stopped after (ownership rule), analyst fallback, else a
   deterministic priority-ranked fallback sheet. The briefing always
   exists; only its intelligence degrades.
4. **Open handoff (the Phase-7 payoff):** open candidates re-enqueue the
   ORIGINAL TriagedSignal on `signal.analyst` with
   `available_ts = session open + 15min` (queue-native delayed delivery —
   the schema had the column since Phase 1). A2 therefore evaluates at
   ~9:45 ET against LIVE opening prices, and C3's existing `open_handoff`
   rule does the gap math: big gap on the news = priced in → veto; small
   gap on high-impact news = the opportunity. **No gate is bypassed or
   changed.** This also closes the ABT-style premarket-earnings miss from
   the 07-16/17 no-trade review — those items now reach A2 with the gap
   visible instead of being measured off an already-moved price.
5. **Briefing email:** ranked sheet + guard/thesis routing + queue stats →
   `journal.outbox` kind `MORNING_BRIEFING` → C5 mails it (~07:30 ET,
   06:30 CT).

## Journal & idempotency

Stage `PREMARKET` (reserved in the Phase-1 schema), agent A4. Actions:
`EXPIRED_BULK`, `GUARD`, `THESIS`, `OPEN_CANDIDATE`, `IGNORE`, `SHEET`,
`SKIPPED_NO_SESSION`. Idempotent per session date via the SHEET decision
(A7 pattern); item-level redelivery no-ops via enqueue dedup keys
(`<item>:<rev>:handoff` / `:a4thesis` / `:a4guard`). Holiday-aware entry
timing via the NYSE calendar (a Saturday run targets Monday 09:45 ET).

## Files

NEW (nothing existing is modified; the a7-heavy sudoers rule from v0.7.0
already covers A4's heavy start/stop — same user, same commands):
`src/a4_premarket/{__init__,schema,prompt,render,service}.py`,
`config/a4.yaml`, `ops/systemd/{a4-premarket.service,a4-premarket.timer}`,
`tests/unit/test_a4_premarket.py`,
`tests/integration/test_premarket_flow.py`, `PATCH_NOTES_v0_8_0.md`,
`DEPLOY-v0_8_0.md`. Plus the pencil edit: `pyproject.toml` → `0.8.0`.

No schema changes. No new sudoers. No env changes.

## Tests

13 new (8 unit + 5 integration... unit: sheet contract + grammar-safety,
deterministic fallback ordering, holiday-aware entry timing incl. the
weekend→Monday-09:45 roll, briefing renderer; integration: full run over a
seeded queue — bulk expiry summary, guard-first routing at priority 0,
thesis lane, verbatim payload handoff with delayed `available_ts` and
rank-derived priority, ignore lane, briefing outbox row, queue fully
drained, same-day rerun no-op, invalid-model fallback). Build environment:
279 green; expect Spark v0.7.0 count + 13 (318).

## Rollback

`sudo systemctl disable --now a4-premarket.timer` + `git checkout v0.7.0`.
The overnight queue resumes accumulating, exactly as before Phase 7.
