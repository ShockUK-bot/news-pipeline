-- Migration 022 — dash_positions shows the CURRENT stop (v0.26.1, 2026-09-23)
--
-- The view read exit_policy.initial_stop.price, so every breakeven, trail and
-- profit-lock ratchet was invisible on the dashboard (FRMI 2026-09-23: stop
-- shown 3.42 while the engine held 5.61). stop_price is now the current stop
-- with the initial stop as fallback; stop_basis and initial_stop are appended.
-- Same rule as migration 015: the FULL live definition, columns appended at
-- the end only (CREATE OR REPLACE VIEW cannot reorder or drop). Safe to re-run.

BEGIN;

CREATE OR REPLACE VIEW journal.dash_positions AS
 SELECT p.position_id AS id,
    p.ticker,
    p.qty_open AS qty,
    p.avg_entry AS entry_price,
    COALESCE(p.last_price, p.avg_entry) AS current_price,
    COALESCE((p.exit_policy ->> 'current_stop')::numeric,
             ((p.exit_policy -> 'initial_stop') ->> 'price')::numeric) AS stop_price,
    ((p.exit_policy -> 'realization') ->> 'price')::numeric AS target_price,
    EXTRACT(epoch FROM p.opened_ts) AS opened_ts,
    EXTRACT(epoch FROM p.closed_ts) AS closed_ts,
    p.status,
    ( SELECT e.exit_layer FROM journal.exits e
       WHERE e.position_id = p.position_id ORDER BY e.ts DESC LIMIT 1) AS exit_reason,
    p.realized_pnl,
    "left"(d.reason, 200) AS thesis,
    p.item_id,
    p.origin,
    round(p.qty_open::numeric * p.avg_entry, 2) AS total_cost,
    round((COALESCE(p.last_price, p.avg_entry) - p.avg_entry) / NULLIF(p.avg_entry, 0::numeric) *
        CASE WHEN p.side = 'SHORT'::text THEN '-100'::integer ELSE 100 END::numeric, 2) AS pct_pnl,
    p.side,
    COALESCE(p.exit_policy ->> 'stop_basis', 'initial') AS stop_basis,
    ((p.exit_policy -> 'initial_stop') ->> 'price')::numeric AS initial_stop
   FROM journal.positions p
     JOIN journal.decisions d ON d.decision_id = p.thesis_decision_id;

ALTER VIEW journal.dash_positions OWNER TO trader;
GRANT SELECT ON journal.dash_positions TO dash_reader;

COMMIT;
