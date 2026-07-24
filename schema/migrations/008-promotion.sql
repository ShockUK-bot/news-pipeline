-- ============================================================================
-- Migration 008 — scanner-trade promotion (v0.12.2)
--
-- Additive: position_events.event_type gains 'PROMOTED' — journaled by C4
-- when a scanner-origin position's causal news prints, A12 verdicts
-- news_confirms_move=true (+ thesis_intact), and code graduates the exit
-- policy from scalp_v1 to short_term_v1 semantics (stop tighten-only,
-- catastrophe untouched, one-way, once).
-- ============================================================================

BEGIN;

ALTER TABLE journal.position_events DROP CONSTRAINT position_events_event_type_check;
ALTER TABLE journal.position_events ADD CONSTRAINT position_events_event_type_check
  CHECK (event_type IN
    ('STOPS_PLACED','BREAKEVEN_MOVED','TRAIL_UPDATED','STOP_TIGHTENED',
     'TIME_STOP_ARMED','INVALIDATION_ARMED','INVALIDATION_FIRED',
     'EARNINGS_BLACKOUT_FLAGGED','OVERNIGHT_HOLD_DECISION',
     'HALT_FROZEN','HALT_RESUMED','SCALE_OUT','EXIT','GUARD_ACTION',
     'CORPORATE_ACTION_ADJ','RECONCILED','FORCE_FLAT','PROMOTED'));

INSERT INTO journal.schema_meta VALUES
  (8, now(), 'Scanner-trade promotion: PROMOTED position event (v0.12.2)');

COMMIT;
