# v0.12.8 — Restore the event vocabulary A6 needs; guard the whole class (2026-08-02)

**Fixes the a6-nightly crash found by the v0.12.7 watchdog on its first
pass** (`LAST_RUN_FAILED`, CheckViolation on `journal.position_events`).
One additive migration, one new static test file. No code changes, no
service restarts.

## The bug — a constraint rebuilt from a stale list

Migration 004 (Phase 8) added `POSITION_REVIEW` and `STALE_FLAG` to the
`position_events_event_type_check` vocabulary — A6's two nightly event
types. Migrations 007 (v0.12.1) and 008 (v0.12.2) each dropped and
re-added that same constraint, but built their value lists from the base
schema file instead of the live constraint — **silently deleting the two
values 004 had added** while adding their own (`FORCE_FLAT`, `PROMOTED`).

Consequences, all latent until a position existed at the right moment:

- **A6 nightly review died on any open position.** JNJ (position 5, the
  first-ever overnight hold) triggered it: the review ran, the model
  produced a verdict ("hold", conf 0.75), and the journal INSERT was
  rejected — job dead, review lost. Confirmed failing 2026-07-31; earlier
  nights' failures were masked by the Jul 28 outage week.
- **A6's STALE_FLAG writes** would fail identically.
- **A live trading path was armed:** the long lane's review-flag-at-target
  (`exits.py` L4, `review_flag` action) journals a `POSITION_REVIEW`
  event when a long-horizon position reaches its target. That write would
  have failed the same way, mid-exit-engine.

Fourth member of the "constraint/vocabulary drift" family (v0.11.7's
dead-man component names, v0.12.5's int(ts), the 0001 NAV migration
outside the sequence). This release also guards the class, not just the
instance.

## The fix

**Migration 009** rebuilds the constraint as the full historical union —
base 16 + `POSITION_REVIEW` + `STALE_FLAG` + `FORCE_FLAT` + `PROMOTED`
(20 values) — and documents the rule in its header: a constraint rebuild
must start from the LIVE definition (`pg_get_constraintdef`), never from
the base schema file.

**New static tests** (`tests/unit/test_schema_vocab.py`, DB-free) make
the mistake un-shippable:

1. The newest migration touching each vocabulary constraint
   (`position_events`, `exits`, `decisions.stage`) must contain the full
   historical union — any future rebuild that drops a value fails the
   suite with a message naming the missing values and the offending file.
2. Every `event_type` literal the code writes (scanned from `src/` by
   regex, including the engine's ratchet map) must be allowed by the
   newest list — new code without a migration fails too.
3. A self-check that the scanner still sees the known writers, so the
   regexes can't silently rot.

Verified both directions in the build environment: with 009 absent the
suite fails exactly as it should ("008-promotion.sql rebuilt … WITHOUT
['POSITION_REVIEW', 'STALE_FLAG']"); with 009 present, **21 passed**
(5 new + the 16 v0.12.7 watchdog tests).

## Files

NEW (4 — nothing existing is modified):
`schema/migrations/009-restore-event-vocab.sql`,
`tests/unit/test_schema_vocab.py`, `patch-notes-v0_12_8.md`,
`v0_12_8-deploy-guide.md`. Plus the pencil edit: `pyproject.toml` →
`0.12.8`. One DB migration (widening-only — every existing row already
satisfies it). No service restarts; A6 picks the fix up on its next
timer fire.

## Rollback

None needed or sensible: the migration only re-adds values that the
schema was always supposed to allow. Rolling code back to v0.12.7 with
migration 009 applied is fully compatible.
