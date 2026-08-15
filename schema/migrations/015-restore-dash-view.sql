-- Migration 015 — restore dash_positions to its full LIVE definition
-- (v0.13.2 hotfix, 2026-08-15)
--
-- WHAT WENT WRONG: migration 013 rebuilt journal.dash_positions from the
-- BASE schema file instead of the live definition — the exact failure mode
-- migration 009 documented for CHECK constraints, applied to a view.
-- Migration 006 had extended the view with origin / total_cost / pct_pnl
-- (dashboard + A13 read all three); 013's rebuild silently dropped them.
-- Observed: dashboard positions show no origin (news|scanner|thesis), no
-- cost, no % P&L since the v0.13.0 deploy.
--
-- THE FIX: rebuild as the FULL union of every column any migration has
-- ever added — 006's complete list, in 006's order, plus 013's `side`
-- APPENDED (append-only keeps positional readers safe and lets future
-- changes use CREATE OR REPLACE again). While here, pct_pnl becomes
-- side-aware: a short's % P&L is positive when the price FELL.
--
-- RULES REAFFIRMED (009's + 014's, now for views too): rebuild any object
-- from its CURRENT LIVE definition, never from the base schema file —
--   SELECT pg_get_viewdef('journal.dash_positions'::regclass);
-- — and every CREATE/DROP+CREATE ends with ALTER ... OWNER TO trader.
--
-- DROP + CREATE (not REPLACE): 013 left `side` mid-list, so the column
-- order must change. Idempotent in effect; safe to re-run.

BEGIN;

DROP VIEW journal.dash_positions;
CREATE VIEW journal.dash_positions AS
SELECT p.position_id                            AS id,
       p.ticker,
       p.qty_open                               AS qty,
       p.avg_entry                              AS entry_price,
       COALESCE(p.last_price, p.avg_entry)      AS current_price,
       (p.exit_policy->'initial_stop'->>'price')::numeric AS stop_price,
       (p.exit_policy->'realization'->>'price')::numeric  AS target_price,
       EXTRACT(EPOCH FROM p.opened_ts)          AS opened_ts,
       EXTRACT(EPOCH FROM p.closed_ts)          AS closed_ts,
       p.status,
       (SELECT e.exit_layer FROM journal.exits e
         WHERE e.position_id = p.position_id
         ORDER BY e.ts DESC LIMIT 1)            AS exit_reason,
       p.realized_pnl,
       LEFT(d.reason, 200)                      AS thesis,
       p.item_id,
       p.origin,                                -- restored (006)
       round(p.qty_open * p.avg_entry, 2)       AS total_cost,   -- restored (006)
       round((COALESCE(p.last_price, p.avg_entry) - p.avg_entry)
             / NULLIF(p.avg_entry, 0)
             * CASE WHEN p.side = 'SHORT' THEN -100 ELSE 100 END,
             2)                                 AS pct_pnl,      -- restored (006),
                                                -- v0.13.2: side-aware sign
       p.side                                   -- appended (013)
FROM journal.positions p
JOIN journal.decisions d ON d.decision_id = p.thesis_decision_id;

ALTER VIEW journal.dash_positions OWNER TO trader;

INSERT INTO journal.schema_meta VALUES
  (15, now(), 'hotfix: dash_positions restored to full live definition (origin/total_cost/pct_pnl back, side appended, side-aware pct_pnl, owner trader)');

COMMIT;
