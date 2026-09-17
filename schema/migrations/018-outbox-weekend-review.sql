-- Migration 018 — outbox kind WEEKEND_REVIEW (v0.18.0, 2026-09-17)
--
-- A9 emails its Saturday review through journal.outbox like A7 and A8.
-- The kind check listed only EOD_REPORT, MORNING_BRIEFING and ALERT.
-- Additive in effect (widens the allowed set); safe to re-run.

BEGIN;

ALTER TABLE journal.outbox DROP CONSTRAINT IF EXISTS outbox_kind_check;
ALTER TABLE journal.outbox ADD CONSTRAINT outbox_kind_check
  CHECK (kind IN ('EOD_REPORT', 'MORNING_BRIEFING', 'ALERT', 'WEEKEND_REVIEW'));

COMMIT;
