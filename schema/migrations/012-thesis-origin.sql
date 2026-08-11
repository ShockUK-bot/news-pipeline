-- ============================================================================
-- Migration 012 — 'thesis' position origin (v0.12.26)
--
-- The C11 thesis-entry lane stamps its positions origin='thesis' so
-- attribution (A11), the dashboard and the journal can separate
-- thesis-store trades from news and scanner trades. Additive: one CHECK
-- widening (the dash_positions view already passes origin through).
-- ============================================================================

BEGIN;

ALTER TABLE journal.positions
  DROP CONSTRAINT positions_origin_check;
ALTER TABLE journal.positions
  ADD CONSTRAINT positions_origin_check
  CHECK (origin IN ('news', 'scanner', 'thesis')) NOT VALID;
ALTER TABLE journal.positions VALIDATE CONSTRAINT positions_origin_check;

INSERT INTO journal.schema_meta VALUES
  (12, now(), 'thesis position origin (v0.12.26)');

COMMIT;
