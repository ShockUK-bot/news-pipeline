-- Migration 003 — outbox delivery accounting (Phase 6: A7 + C5).
-- The Phase-1 schema already created journal.outbox (message_id, kind,
-- subject, body, fact_sheet, status QUEUED/SENT/FAILED). This migration is
-- ADDITIVE ONLY (§11.5 policy): retry accounting + decision lineage for the
-- C5 mailer. Idempotent; apply to trading AND trading_test.

ALTER TABLE journal.outbox
  ADD COLUMN IF NOT EXISTS attempts    SMALLINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_error  TEXT,
  ADD COLUMN IF NOT EXISTS decision_id BIGINT REFERENCES journal.decisions(decision_id);
