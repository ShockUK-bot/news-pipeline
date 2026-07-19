-- ============================================================================
-- Migration 004 — Phase 8 thesis store (journal schema v4)
--
-- Additive per the schema migration policy: two new tables, one sequence,
-- one view, one CHECK-constraint widening (decisions.stage gains 'THEMATIC'
-- for A5 — 'POSITION_REVIEW' for A6 was reserved in the Phase-1 schema),
-- and a schema_meta row. No destructive changes.
--
-- DEPLOYMENT NOTE: the constraint swap on journal.decisions takes an
-- ACCESS EXCLUSIVE lock for the DROP + ADD. The ADD uses NOT VALID +
-- VALIDATE so the full-table scan runs under the lighter SHARE UPDATE
-- EXCLUSIVE lock. Run outside RTH, against trading_test first (repo
-- deploy loop). Apply to trading AND trading_test.
-- ============================================================================

BEGIN;

SET search_path TO journal;

-- ----------------------------------------------------------------------------
-- 1. The persistent thesis store (baseline §6.3: driver, beneficiaries,
--    dated evidence log, confidence, invalidation conditions). A5 is the
--    only writer; A1's router, A2's context pack, A4, A6 and A8 read it.
-- ----------------------------------------------------------------------------

CREATE TABLE theses (
  thesis_id       TEXT PRIMARY KEY,          -- 'th-2026-001', code-generated
  created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  status          TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE','EXPIRED','INVALIDATED','REALIZED')),
  title           TEXT NOT NULL,             -- short label ("Grid capex supercycle")
  driver          TEXT NOT NULL,             -- the causal mechanism, 1-3 sentences
  direction       TEXT NOT NULL CHECK (direction IN ('up','down','unclear')),
  horizon         TEXT NOT NULL DEFAULT 'LONG' CHECK (horizon IN ('SHORT','LONG')),
  confidence      REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
  beneficiaries   JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{ticker,relation,rationale}]
  invalidation    JSONB NOT NULL DEFAULT '[]'::jsonb,  -- news-checkable conditions [str]
  last_evidence_ts TIMESTAMPTZ,              -- staleness clock (A5 expiry rule)
  evidence_count  INTEGER NOT NULL DEFAULT 0,
  created_decision_id BIGINT REFERENCES decisions(decision_id),
  status_decision_id  BIGINT REFERENCES decisions(decision_id),  -- latest change
  config_version  TEXT NOT NULL REFERENCES config_versions(config_version),
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_theses_status ON theses (status, updated_ts DESC);

-- Dated evidence log: one row per (thesis, item revision) — redelivery is a
-- no-op via the UNIQUE constraint, mirroring queue-level enqueue dedup.
CREATE TABLE thesis_evidence (
  evidence_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  thesis_id       TEXT NOT NULL REFERENCES theses(thesis_id),
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  item_id         TEXT NOT NULL,             -- news-store reference (no cross-store FK)
  item_revision   SMALLINT NOT NULL DEFAULT 1,
  polarity        TEXT NOT NULL CHECK (polarity IN ('SUPPORTS','CONTRADICTS','NEUTRAL')),
  note            TEXT,                      -- model's one-liner: why this matters
  decision_id     BIGINT REFERENCES decisions(decision_id),
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (thesis_id, item_id, item_revision)
);
CREATE INDEX idx_thev_thesis ON thesis_evidence (thesis_id, ts DESC);

-- Code-generated thesis ids ('th-<year>-<n>'); the model never mints ids.
CREATE SEQUENCE thesis_seq;

-- The ranked watchlist surface (baseline: "consumed by A1 and A8"): one row
-- per (active thesis, beneficiary ticker). router.facts.thesis_matches and
-- the Phase-9 A8 briefing read this view.
CREATE VIEW thesis_watchlist AS
  SELECT b->>'ticker'        AS ticker,
         t.thesis_id,
         t.title,
         t.direction,
         t.confidence,
         b->>'relation'      AS relation
  FROM theses t, jsonb_array_elements(t.beneficiaries) b
  WHERE t.status = 'ACTIVE' AND (b ? 'ticker');

-- ----------------------------------------------------------------------------
-- 2. decisions.stage gains 'THEMATIC' — A5's rows. (A6 uses the reserved
--    'POSITION_REVIEW' stage; no change needed there.)
-- ----------------------------------------------------------------------------

-- position_events gains A6's two nightly event types ('OVERNIGHT_HOLD_DECISION'
-- for the EOD check was already reserved in the Phase-1 schema).
ALTER TABLE position_events DROP CONSTRAINT position_events_event_type_check;
ALTER TABLE position_events ADD CONSTRAINT position_events_event_type_check
  CHECK (event_type IN
    ('STOPS_PLACED','BREAKEVEN_MOVED','TRAIL_UPDATED','STOP_TIGHTENED',
     'TIME_STOP_ARMED','INVALIDATION_ARMED','INVALIDATION_FIRED',
     'EARNINGS_BLACKOUT_FLAGGED','OVERNIGHT_HOLD_DECISION',
     'HALT_FROZEN','HALT_RESUMED','SCALE_OUT','EXIT','GUARD_ACTION',
     'CORPORATE_ACTION_ADJ','RECONCILED',
     'POSITION_REVIEW','STALE_FLAG')) NOT VALID;
ALTER TABLE position_events VALIDATE CONSTRAINT position_events_event_type_check;

ALTER TABLE decisions DROP CONSTRAINT decisions_stage_check;
ALTER TABLE decisions ADD CONSTRAINT decisions_stage_check CHECK (stage IN
  ('TRIAGE','ANALYST','GATE','RISK','ORDER','GUARD',
   'PREMARKET','POSITION_REVIEW','SYSTEM','CHAT','THEMATIC')) NOT VALID;
ALTER TABLE decisions VALIDATE CONSTRAINT decisions_stage_check;

-- Ownership: no-op when run as trader; required when run as postgres so the
-- pipeline (user trader) can write the new tables.
ALTER TABLE theses OWNER TO trader;
ALTER TABLE thesis_evidence OWNER TO trader;
ALTER SEQUENCE thesis_seq OWNER TO trader;
ALTER VIEW thesis_watchlist OWNER TO trader;

INSERT INTO schema_meta VALUES
  (4, now(), 'Phase 8: theses/thesis_evidence/thesis_watchlist + thesis_seq, THEMATIC stage; position_events event types POSITION_REVIEW/STALE_FLAG');

COMMIT;
