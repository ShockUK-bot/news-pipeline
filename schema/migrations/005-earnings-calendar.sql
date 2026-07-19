-- ============================================================================
-- Migration 005 — Earnings calendar store (news schema; journal meta v5)
--
-- Additive per the schema migration policy: one new table in the news
-- schema (it is ingested source data, not a decision record) and a
-- schema_meta row. No destructive changes. Closes the D7 "earnings
-- calendar (P1)" deferral: A3's EARNINGS_UNKNOWN flag becomes a real
-- blackout veto once rows exist.
-- ============================================================================

BEGIN;

CREATE TABLE news.earnings_calendar (
  ticker          TEXT NOT NULL,
  report_date     DATE NOT NULL,
  session         TEXT NOT NULL DEFAULT 'UNKNOWN'
                    CHECK (session IN ('BMO','AMC','UNKNOWN')),
  eps_estimate    NUMERIC(12,4),             -- nullable; provider-dependent
  fiscal_ending   DATE,                      -- fiscalDateEnding when provided
  source          TEXT NOT NULL,             -- 'alphavantage' | ...
  fetched_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  PRIMARY KEY (ticker, report_date)
);
CREATE INDEX idx_earnings_next ON news.earnings_calendar (ticker, report_date);
CREATE INDEX idx_earnings_date ON news.earnings_calendar (report_date);

ALTER TABLE news.earnings_calendar OWNER TO trader;

INSERT INTO journal.schema_meta VALUES
  (5, now(), 'Earnings calendar: news.earnings_calendar (D7 P1 source; A3 blackout + A2 context)');

COMMIT;
