-- ============================================================================
-- Migration 011 — Macro/economy series store (news schema; meta v11)
--
-- v0.12.24: the system's first non-equity input. One table of dated
-- observations for the configured FRED series (rates, curve, inflation,
-- labor, growth, credit, dollar, energy). Ingested source data, not a
-- decision record, so it lives in the news schema like earnings_calendar.
-- Additive only; no constraint changes; journal stage vocabulary untouched
-- (the refresh journals under the existing SYSTEM stage).
-- ============================================================================

BEGIN;

CREATE TABLE news.macro_series (
  series_id       TEXT NOT NULL,             -- FRED id, e.g. 'DGS10'
  obs_date        DATE NOT NULL,             -- observation date (source's)
  value           NUMERIC(18,6) NOT NULL,    -- missing obs ('.') never stored
  source          TEXT NOT NULL,             -- 'fredgraph' | 'fred-api'
  fetched_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  PRIMARY KEY (series_id, obs_date)
);
CREATE INDEX idx_macro_series_latest ON news.macro_series (series_id, obs_date DESC);

ALTER TABLE news.macro_series OWNER TO trader;

INSERT INTO journal.schema_meta VALUES
  (11, now(), 'macro series store (v0.12.24)');

COMMIT;
