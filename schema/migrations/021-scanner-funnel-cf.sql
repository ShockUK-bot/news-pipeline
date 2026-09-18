-- Migration 021 — journal.scanner_funnel_cf (v0.23.0, 2026-09-18)
--
-- One row per scanner candidate that was emitted or capped but did NOT
-- become a position: what happened to it (capped, gate veto, size clipped,
-- analyst no-trade, shadow short) and what a scalp_v1 trade from the
-- detection minute would have done in BOTH directions (minute-bar replay,
-- same ladder as the live lane). Written nightly by A11. Research only.
-- Additive; safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS journal.scanner_funnel_cf (
    candidate_id     bigint      PRIMARY KEY REFERENCES journal.scanner_candidates(candidate_id),
    scan_date        date        NOT NULL,
    ticker           text        NOT NULL,
    detect_ts        timestamptz NOT NULL,
    move_direction   text        NOT NULL,      -- up / down (the scanner's move)
    outcome          text        NOT NULL,      -- CAPPED_CONCURRENT, CAPPED_PER_SCAN, CAPPED_PER_HOUR,
                                                -- GATE_VETO, RISK_VETO, ANALYST_REJECT, SHADOW_SHORT, NO_DECISION
    reason           text,                      -- veto reason or reject text
    analyst_direction text,                     -- up / down / null (what A2 proposed)
    entry_px         numeric(14,4) NOT NULL,
    atr_5m           numeric(12,4) NOT NULL,
    long_r           numeric(8,3)  NOT NULL,
    long_pnl         numeric(12,2) NOT NULL,
    short_r          numeric(8,3)  NOT NULL,
    short_pnl        numeric(12,2) NOT NULL,
    with_move_r      numeric(8,3)  NOT NULL,    -- the momentum-direction trade
    long_exits       jsonb NOT NULL DEFAULT '[]'::jsonb,
    short_exits      jsonb NOT NULL DEFAULT '[]'::jsonb,
    qty              integer NOT NULL,          -- shares at the lane's notional
    computed_ts      timestamptz NOT NULL DEFAULT now(),
    schema_version   smallint NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_funnel_cf_date ON journal.scanner_funnel_cf (scan_date DESC);
ALTER TABLE journal.scanner_funnel_cf OWNER TO trader;
GRANT SELECT ON journal.scanner_funnel_cf TO dash_reader;

COMMIT;
