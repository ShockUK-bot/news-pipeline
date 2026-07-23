-- ============================================================================
-- Migration 006 — Position origin + open-position dashboard columns (v0.12.0)
--
-- Additive per the schema migration policy:
--   1. journal.positions.origin — which INPUT produced the trade:
--        'news'    — the news pipeline (every existing row; the DEFAULT)
--        'scanner' — the C10 momentum scanner (stamped from v0.12.1 on)
--      Sympathy-lane trades remain origin='news' (their lineage is
--      decisions.derived_from; the origin axis is news-vs-tape, not
--      first-vs-second order).
--   2. dash_positions gains origin, total_cost (qty_open x avg_entry) and
--      pct_pnl (unrealized % vs entry) — computed in the view so every
--      consumer (index.html, A13 chat) reads identical numbers.
--      CREATE OR REPLACE only APPENDS columns, so existing readers are
--      untouched.
-- ============================================================================

BEGIN;

ALTER TABLE journal.positions
  ADD COLUMN origin TEXT NOT NULL DEFAULT 'news'
    CHECK (origin IN ('news', 'scanner'));

CREATE OR REPLACE VIEW journal.dash_positions AS
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
       p.origin,
       round(p.qty_open * p.avg_entry, 2)       AS total_cost,
       round((COALESCE(p.last_price, p.avg_entry) - p.avg_entry)
             / NULLIF(p.avg_entry, 0) * 100, 2) AS pct_pnl
FROM journal.positions p
JOIN journal.decisions d ON d.decision_id = p.thesis_decision_id;

INSERT INTO journal.schema_meta VALUES
  (6, now(), 'Position origin (news|scanner) + dash_positions origin/total_cost/pct_pnl (v0.12.0)');

COMMIT;
