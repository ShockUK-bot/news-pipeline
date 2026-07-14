-- Migration: portfolio_nav_daily (C6 v1.3 Performance tab)
-- Additive only — no existing table/view is touched. Safe to run live.
SET search_path TO journal;

CREATE TABLE portfolio_nav_daily (
  nav_date          DATE PRIMARY KEY,
  realized_pnl_cum  NUMERIC(14,4) NOT NULL,
  unrealized_pnl    NUMERIC(14,4) NOT NULL,
  total_pnl         NUMERIC(14,4) NOT NULL,
  schema_version    SMALLINT NOT NULL DEFAULT 1
);

-- performance_baseline_capital is a normal row in the existing journal.control
-- table (no DDL needed) — ops/snapshot_nav.py sets it once, the first time it
-- runs after a trade exists, and never overwrites it again.
