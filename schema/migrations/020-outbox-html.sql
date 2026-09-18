-- Migration 020 — journal.outbox html part + EVENING_DIGEST kind (v0.22.0, 2026-09-17)
-- The mailer sends html as a multipart alternative when present; body stays
-- the plain-text fallback. Additive; safe to re-run.
BEGIN;
ALTER TABLE journal.outbox ADD COLUMN IF NOT EXISTS html text;
ALTER TABLE journal.outbox DROP CONSTRAINT IF EXISTS outbox_kind_check;
ALTER TABLE journal.outbox ADD CONSTRAINT outbox_kind_check
  CHECK (kind IN ('EOD_REPORT', 'MORNING_BRIEFING', 'ALERT', 'WEEKEND_REVIEW', 'EVENING_DIGEST'));
COMMIT;
