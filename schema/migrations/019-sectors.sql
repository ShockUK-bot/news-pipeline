-- Migration 019 — journal.sectors (v0.19.0, 2026-09-17)
--
-- Ticker -> sector from SEC SIC codes (EDGAR submissions), one row per
-- ticker the pipeline has touched. Unknowns are stored too (sector NULL,
-- source no_cik / edgar_submissions) so a miss is not refetched on every
-- sizing call. Activates the A3 sector-heat clip and fills the analyst's
-- `sector` context slot. Additive; safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS journal.sectors (
    ticker          text        PRIMARY KEY,
    cik             bigint,
    sic             integer,
    sic_description text,
    sector          text,
    name            text,
    source          text        NOT NULL DEFAULT 'edgar_submissions',
    updated_ts      timestamptz NOT NULL DEFAULT now(),
    schema_version  smallint    NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sectors_sector ON journal.sectors (sector);
ALTER TABLE journal.sectors OWNER TO trader;
GRANT SELECT ON journal.sectors TO dash_reader;

COMMIT;
