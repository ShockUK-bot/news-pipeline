-- Migration 009 — restore POSITION_REVIEW / STALE_FLAG to position_events
-- (v0.12.8, 2026-08-02)
--
-- WHAT WENT WRONG: migration 004 (Phase 8) added 'POSITION_REVIEW' and
-- 'STALE_FLAG' to position_events_event_type_check. Migrations 007 and 008
-- then each DROPPED and re-ADDED that constraint from a list copied off the
-- base schema file instead of the live definition — silently DELETING the
-- two values 004 had added. Result: since v0.12.1 every A6 nightly review
-- with an open position crashed on CheckViolation (first observed on JNJ,
-- caught by the v0.12.7 watchdog's LAST_RUN_FAILED check on 2026-08-02),
-- and the long lane's review-flag-at-target write would have failed the
-- same way.
--
-- THE FIX: rebuild the constraint as the full UNION of every value any
-- migration has ever added: base 16 + 004's POSITION_REVIEW/STALE_FLAG +
-- 007's FORCE_FLAT + 008's PROMOTED = 20 values.
--
-- RULE FOR ALL FUTURE MIGRATIONS (the lesson): when rebuilding a CHECK
-- constraint, start from the CURRENT LIVE definition —
--   SELECT pg_get_constraintdef(oid) FROM pg_constraint
--   WHERE conname = '<constraint name>';
-- — never from the base schema file. tests/unit/test_schema_vocab.py now
-- enforces the union mechanically: any migration that shrinks a vocabulary
-- fails the suite.

BEGIN;

ALTER TABLE journal.position_events
  DROP CONSTRAINT position_events_event_type_check;
ALTER TABLE journal.position_events
  ADD CONSTRAINT position_events_event_type_check
  CHECK (event_type IN
    ('STOPS_PLACED','BREAKEVEN_MOVED','TRAIL_UPDATED','STOP_TIGHTENED',
     'TIME_STOP_ARMED','INVALIDATION_ARMED','INVALIDATION_FIRED',
     'EARNINGS_BLACKOUT_FLAGGED','OVERNIGHT_HOLD_DECISION',
     'HALT_FROZEN','HALT_RESUMED','SCALE_OUT','EXIT','GUARD_ACTION',
     'CORPORATE_ACTION_ADJ','RECONCILED',
     'POSITION_REVIEW','STALE_FLAG','FORCE_FLAT','PROMOTED'));

INSERT INTO journal.schema_meta VALUES
  (9, now(),
   'Restore POSITION_REVIEW/STALE_FLAG lost in the 007/008 constraint rebuilds (v0.12.8)');

COMMIT;
