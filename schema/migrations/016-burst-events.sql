-- Migration 016 — journal.burst_events (v0.15.0, 2026-09-15)
--
-- C12 burst stream: every intraday burst detected on the real-time trade
-- stream, one row per rule (momentum / fade / news_anchored share a
-- detection), with the forward path filled in from memory 30 minutes
-- later. Research table: no trading reads it. Additive; safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS journal.burst_events (
    event_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol          text        NOT NULL,
    ts              timestamptz NOT NULL,
    rule            text        NOT NULL,        -- momentum | fade | news_anchored
    direction       text        NOT NULL CHECK (direction IN ('up','down')),
    price           numeric(14,4) NOT NULL,      -- last trade at detection
    ret_60s         numeric(9,5),
    ret_120s        numeric(9,5),
    vol_mult        numeric(9,3),
    spread_bps      numeric(9,2),
    new_extreme     boolean     NOT NULL DEFAULT false,
    news_anchored   boolean     NOT NULL DEFAULT false,
    news_direction  text,
    scanner_known   boolean     NOT NULL DEFAULT false,
    feed            text,
    detail          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- forward path (filled at +30 min)
    p_1m            numeric(14,4),
    p_5m            numeric(14,4),
    p_15m           numeric(14,4),
    p_30m           numeric(14,4),
    max_fav_pct     numeric(9,5),
    max_adv_pct     numeric(9,5),
    first_hit       text,                        -- target | stop | none
    first_hit_min   numeric(8,2),
    complete        boolean     NOT NULL DEFAULT false,
    created_ts      timestamptz NOT NULL DEFAULT now(),
    schema_version  smallint    NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_burst_ts ON journal.burst_events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_burst_symbol_ts ON journal.burst_events (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_burst_pending ON journal.burst_events (complete) WHERE NOT complete;
ALTER TABLE journal.burst_events OWNER TO trader;
GRANT SELECT ON journal.burst_events TO dash_reader;

COMMIT;
