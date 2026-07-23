-- ============================================================================
-- Migration 007 — C10 momentum scanner (v0.12.1)
--
-- Additive per the schema migration policy:
--   1. journal.scanner_candidates — every ticker C10 *sees*, including the
--      ones it filters out (A9's threshold-tuning evidence; baseline rule 5:
--      everything is logged).
--   2. journal.control seed 'scanner_enabled' (default ON; dashboard toggle
--      flips it — C10 checks it every scan, code enforces as always).
--   3. exits.exit_layer CHECK gains 'FORCE_FLAT' (the scalp lane's hard
--      no-overnight exit) and position_events.event_type gains 'FORCE_FLAT'.
-- ============================================================================

BEGIN;

CREATE TABLE journal.scanner_candidates (
  candidate_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  scan_date       DATE NOT NULL DEFAULT (now() AT TIME ZONE 'America/New_York')::date,
  ticker          TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN
                    ('EMITTED',            -- passed filters, signal enqueued
                     'FILTERED',           -- failed a deterministic filter
                     'SUPPRESSED_NEWS',    -- news pipeline already owns it
                     'CAPPED',             -- would pass, but a cap said no
                     'BREAKER')),          -- scanner circuit breaker active
  reject_reason   TEXT,                    -- filter/cap code when not EMITTED
  metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- full detection snapshot
  item_id         TEXT,                    -- scanner item id when EMITTED
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_scanner_cand_day ON journal.scanner_candidates (scan_date, ticker);
CREATE INDEX idx_scanner_cand_ts  ON journal.scanner_candidates (ts DESC);
ALTER TABLE journal.scanner_candidates OWNER TO trader;

INSERT INTO journal.control (key, value, updated_ts)
VALUES ('scanner_enabled', '1', now())
ON CONFLICT (key) DO NOTHING;

ALTER TABLE journal.exits DROP CONSTRAINT exits_exit_layer_check;
ALTER TABLE journal.exits ADD CONSTRAINT exits_exit_layer_check
  CHECK (exit_layer IN
    ('STOP','CATASTROPHE','BREAKEVEN','TRAIL','TIME','TARGET',
     'INVALIDATION','GUARD','REVIEW','EARNINGS','OVERNIGHT',
     'BREAKER','KILL','OPERATOR','FORCE_FLAT'));

ALTER TABLE journal.position_events DROP CONSTRAINT position_events_event_type_check;
ALTER TABLE journal.position_events ADD CONSTRAINT position_events_event_type_check
  CHECK (event_type IN
    ('STOPS_PLACED','BREAKEVEN_MOVED','TRAIL_UPDATED','STOP_TIGHTENED',
     'TIME_STOP_ARMED','INVALIDATION_ARMED','INVALIDATION_FIRED',
     'EARNINGS_BLACKOUT_FLAGGED','OVERNIGHT_HOLD_DECISION',
     'HALT_FROZEN','HALT_RESUMED','SCALE_OUT','EXIT','GUARD_ACTION',
     'CORPORATE_ACTION_ADJ','RECONCILED','FORCE_FLAT'));

INSERT INTO journal.schema_meta VALUES
  (7, now(), 'C10 scanner: scanner_candidates, scanner_enabled control, FORCE_FLAT exit layer (v0.12.1)');

COMMIT;
