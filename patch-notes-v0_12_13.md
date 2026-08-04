# v0.12.13 — pre-market late pass (2026-08-04)

## The bug this fixes, with its victim

The overnight queue (`signal.overnight`) was drained exactly once per day:
the A4 sheet run at 07:00 ET. Anything escalated between 07:00 and the
09:30 open — the richest stretch of pre-market, where earnings reactions
and analyst-rating stories land — sat unread until the NEXT morning and
died of staleness.

Named victim (2026-08-04): PLTR, "analysts turning bullish," escalated at
07:46 ET on the morning the stock ran all day. Confirmed in the queue with
`done_ts` NULL at end of day. The 07:00 sheet had already run; nothing
looked again.

## What it does

A new oneshot, `a4_premarket/late.py`, fired by **a4-late.timer** every 10
minutes 07:00–09:59 ET on weekdays:

1. **Guards (all silent no-ops):** today's SHEET decision must exist (the
   late pass never front-runs or replaces the 07:00 ranked sheet), the
   market must not have opened yet (once open, router rule 4 already sends
   news straight to the analyst), and the upcoming open must be TODAY'S
   open (an evening manual start cannot drain the queue prematurely).
   Holidays no-op automatically: the sheet journals SKIPPED_NO_SESSION,
   never SHEET.
2. **Code-routes exactly like the sheet run:** open-position tickers →
   `signal.guard` (A12); ticker-less material items → `signal.thesis`
   (A5's nightly thematic lane).
3. **Forwards the best fresh candidates to the open:** ordered by queue
   priority (A1's priority_score), re-enqueued on `signal.analyst` with
   `available_ts` = open + blackout — the identical delayed open-handoff
   and dedup key the sheet's open_candidates use, so nothing can be
   forwarded twice. Each is journaled **PREMARKET | LATE_CANDIDATE**.
4. **Bounded and honest:** at most `sheet.late_daily_max` (default 10)
   late candidates per day across all passes, counted from the journal.
   Over-budget items journal PREMARKET | IGNORE with the reason — visible,
   never silent. No model call anywhere: the late pass is deterministic;
   A2 and C3 remain the judges, at live opening prices.

Thresholds, gate rules, and risk checks are untouched. This release only
guarantees fresh pre-market signals REACH them — the same philosophy as
v0.12.10's re-check window.

## What it deliberately does NOT change

- `a4_premarket/service.py` (the 07:00 sheet run): byte-identical.
- The eh-shadow branch (v0.12.11): unchanged; a 07:46 signal now gets BOTH
  an immediate shadow evaluation AND a real, delayed-to-open evaluation.
- `signal.thesis`: confirmed tonight to be A5's healthy nightly lane
  (21:30 ET), not a dead end — no change needed or made.

## Files

NEW (6): `src/a4_premarket/late.py`, `tests/unit/test_a4_late.py`,
`ops/systemd/a4-late.service`, `ops/systemd/a4-late.timer`, these patch
notes, the deploy guide.
REPLACED (1): `config/a4.yaml` (adds `sheet.late_daily_max: 10`).

Release test set: 50 green (test_a4_late, test_a4_premarket,
test_triage_router, test_a5_thematic). No migration. No service restarts —
one new timer to install and enable.

## Rollback

```bash
sudo systemctl disable --now a4-late.timer
sudo -u trader git -C /opt/pipeline checkout v0.12.12
```
