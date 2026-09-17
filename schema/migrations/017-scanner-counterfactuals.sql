-- Migration 017 — journal.scanner_counterfactuals (v0.16.0, 2026-09-17)
--
-- One row per closed scanner position per exit-ladder variant, written by
-- the nightly scanner-counterfactual job (ops/scanner_counterfactuals.py)
-- from a minute-bar replay of scalp_v1 with one parameter changed. Answers
-- "would no scale-out / a 3.0 ATR stop / no time stop have done better"
-- from evidence instead of one-off replays. The same job fills the
-- previously empty journal.trade_metrics. Research table: nothing trades
-- from it. Additive; safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS journal.scanner_counterfactuals (
    position_id     bigint      NOT NULL REFERENCES journal.positions(position_id),
    variant         text        NOT NULL,      -- base, noscale, runner_hold, notime, stop3.0, trail2.5, trail3.0
    pnl             numeric(12,2) NOT NULL,    -- replay P&L at the real share count
    r_multiple      numeric(8,3) NOT NULL,     -- against the variant's own R unit
    mfe_r           numeric(8,3) NOT NULL,     -- max favourable excursion, base R unit
    close_r         numeric(8,3),              -- unrealised at 14:50 CT had nothing exited
    exits           jsonb       NOT NULL DEFAULT '[]'::jsonb,   -- [[hhmm, layer, qty, px], ...]
    bars            integer     NOT NULL,
    computed_ts     timestamptz NOT NULL DEFAULT now(),
    schema_version  smallint    NOT NULL DEFAULT 1,
    PRIMARY KEY (position_id, variant)
);
ALTER TABLE journal.scanner_counterfactuals OWNER TO trader;
GRANT SELECT ON journal.scanner_counterfactuals TO dash_reader;

COMMIT;
