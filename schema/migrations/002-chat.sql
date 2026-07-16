-- ============================================================================
-- Migration 002 — A13 Operator Chat (journal schema v2)
--
-- Additive per the schema migration policy: new tables, one CHECK-constraint
-- widening (decisions.stage gains 'CHAT'), one new dashboard view, and a
-- schema_meta row. No destructive changes.
--
-- DEPLOYMENT NOTE: the constraint swap on journal.decisions takes an
-- ACCESS EXCLUSIVE lock for the DROP + ADD. The ADD uses NOT VALID +
-- VALIDATE so the full-table scan runs under the lighter SHARE UPDATE
-- EXCLUSIVE lock, but every pipeline stage writes decisions during RTH —
-- run this AFTER 16:00 ET, against trading_test first (repo deploy loop).
-- ============================================================================

BEGIN;

SET search_path TO journal;

-- ----------------------------------------------------------------------------
-- 1. Chat tables. The dashboard writes OPERATOR rows and enqueues chat.request;
--    A13 writes ASSISTANT rows. Nothing else touches these tables.
-- ----------------------------------------------------------------------------

CREATE TABLE chat_sessions (
  session_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  title           TEXT,                      -- first question, truncated (UI label)
  schema_version  SMALLINT NOT NULL DEFAULT 2
);

CREATE TABLE chat_messages (
  message_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id      BIGINT NOT NULL REFERENCES chat_sessions(session_id),
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  role            TEXT NOT NULL CHECK (role IN ('OPERATOR','ASSISTANT','SYSTEM')),
  kind            TEXT NOT NULL DEFAULT 'ASK'
                    CHECK (kind IN ('ASK','ANSWER','FILE_REQUEST','FILE_RESULT','ERROR')),
  content         TEXT NOT NULL,
  reply_to        BIGINT REFERENCES chat_messages(message_id),
  fact_sheet      JSONB,          -- code-computed retrieval pack the answer was
                                  -- generated from (rule 5: numbers come from code)
  proposal        JSONB,          -- filing_proposal on ANSWER rows; echoed verbatim
                                  -- on the FILE_REQUEST that confirms it
  decision_id     BIGINT REFERENCES decisions(decision_id),  -- CHAT/FILED lineage
  status          TEXT NOT NULL DEFAULT 'DONE'
                    CHECK (status IN ('PENDING','DONE','ERROR')),
  model_id        TEXT,
  latency_ms      INTEGER,
  schema_version  SMALLINT NOT NULL DEFAULT 2
);
CREATE INDEX idx_chat_session ON chat_messages (session_id, message_id);
CREATE INDEX idx_chat_pending ON chat_messages (message_id) WHERE status = 'PENDING';

-- ----------------------------------------------------------------------------
-- 2. decisions.stage gains 'CHAT' — A13's FILED decisions (operator-initiated
--    evaluation requests). NOT VALID + VALIDATE keeps the heavy lock brief.
-- ----------------------------------------------------------------------------

ALTER TABLE decisions DROP CONSTRAINT decisions_stage_check;
ALTER TABLE decisions ADD CONSTRAINT decisions_stage_check CHECK (stage IN
  ('TRIAGE','ANALYST','GATE','RISK','ORDER','GUARD',
   'PREMARKET','POSITION_REVIEW','SYSTEM','CHAT')) NOT VALID;
ALTER TABLE decisions VALIDATE CONSTRAINT decisions_stage_check;

-- ----------------------------------------------------------------------------
-- 3. Dashboard read shape (c6-dashboard-spec v1.3 §6)
-- ----------------------------------------------------------------------------

CREATE VIEW dash_chat AS
SELECT message_id                       AS id,
       session_id,
       EXTRACT(EPOCH FROM ts)           AS ts,
       role,
       kind,
       content,
       reply_to,
       proposal,
       decision_id,
       status,
       model_id,
       latency_ms
FROM chat_messages;

-- ----------------------------------------------------------------------------
-- 4. Bookkeeping
-- ----------------------------------------------------------------------------

INSERT INTO schema_meta VALUES
  (2, now(), 'A13 operator chat: chat_sessions/chat_messages, CHAT stage, dash_chat view');

COMMIT;
