-- Migration 013 — short selling (v0.13.0, 2026-08-14)
--
-- Additive only. Four changes:
--   1. intents.side gains the two short sides: SELL_SHORT (short entry)
--      and BUY_TO_COVER (short exit). BUY/SELL semantics are untouched.
--   2. positions gains `side` ('LONG'|'SHORT'), DEFAULT 'LONG' so every
--      existing row backfills correctly. NOTE the vocabulary trap: the
--      neighbouring `horizon` column also uses the strings SHORT/LONG but
--      means TIME (intraday-days vs weeks-months). `side` means DIRECTION.
--   3. exits.exit_layer gains 'DIVIDEND' (short exited before ex-dividend
--      date) and 'BORROW' (short exited because the name left the
--      easy-to-borrow list). Rebuilt from the FULL live union per the
--      migration-009 rule (base 14 + 007's FORCE_FLAT + these two).
--   4. position_events.event_type gains 'BORROW_LOST' (daily borrow
--      re-check found an open short no longer ETB). Rebuilt from the FULL
--      live union (009's 20 values + this one).
--
-- r_unit stays CHECK (r_unit > 0): for shorts the stop is ABOVE entry and
-- C4 computes r_unit = initial_stop - avg_entry, which is positive. No
-- constraint change needed — the invariant is enforced in
-- src/common/direction.py::r_unit().
--
-- Shadow-mode shorts write decisions + gate_counterfactuals rows only and
-- need no schema change at all (rule 'shadow_short' is free text).

BEGIN;

-- 1. intents.side ------------------------------------------------------------
ALTER TABLE journal.intents
  DROP CONSTRAINT intents_side_check;
ALTER TABLE journal.intents
  ADD CONSTRAINT intents_side_check
  CHECK (side IN ('BUY','SELL','SELL_SHORT','BUY_TO_COVER'));

-- 2. positions.side ----------------------------------------------------------
ALTER TABLE journal.positions
  ADD COLUMN side TEXT NOT NULL DEFAULT 'LONG'
  CHECK (side IN ('LONG','SHORT'));

-- 3. exits.exit_layer (full union: base + 007 FORCE_FLAT + DIVIDEND,BORROW) --
ALTER TABLE journal.exits DROP CONSTRAINT exits_exit_layer_check;
ALTER TABLE journal.exits ADD CONSTRAINT exits_exit_layer_check
  CHECK (exit_layer IN
    ('STOP','CATASTROPHE','BREAKEVEN','TRAIL','TIME','TARGET',
     'INVALIDATION','GUARD','REVIEW','EARNINGS','OVERNIGHT',
     'BREAKER','KILL','OPERATOR',
     'FORCE_FLAT',                            -- added 007
     'DIVIDEND','BORROW'));                   -- added 013

-- 3b. dash_positions view gains the side column (dashboard renders it).
-- DROP + CREATE (not CREATE OR REPLACE): inserting a column mid-list
-- changes the view's column order, which REPLACE refuses.
DROP VIEW journal.dash_positions;
CREATE VIEW journal.dash_positions AS
SELECT p.position_id                            AS id,
       p.ticker,
       p.side,                                  -- v0.13
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
        ORDER BY e.ts DESC LIMIT 1)             AS exit_reason,
       p.realized_pnl,
       LEFT(d.reason, 200)                      AS thesis,
       p.item_id
FROM journal.positions p
JOIN journal.decisions d ON d.decision_id = p.thesis_decision_id;

-- 4. position_events.event_type (full union: 009's 20 + BORROW_LOST) ---------
ALTER TABLE journal.position_events
  DROP CONSTRAINT position_events_event_type_check;
ALTER TABLE journal.position_events
  ADD CONSTRAINT position_events_event_type_check
  CHECK (event_type IN
    ('STOPS_PLACED','BREAKEVEN_MOVED','TRAIL_UPDATED','STOP_TIGHTENED',
     'TIME_STOP_ARMED','INVALIDATION_ARMED','INVALIDATION_FIRED',
     'EARNINGS_BLACKOUT_FLAGGED','OVERNIGHT_HOLD_DECISION',
     'HALT_FROZEN','HALT_RESUMED','SCALE_OUT','EXIT','GUARD_ACTION',
     'CORPORATE_ACTION_ADJ','RECONCILED',
     'POSITION_REVIEW','STALE_FLAG',          -- added 004, restored 009
     'FORCE_FLAT',                            -- added 007
     'PROMOTED',                              -- added 008
     'BORROW_LOST'));                         -- added 013

INSERT INTO journal.schema_meta VALUES
  (13, now(), 'shorting: intent short sides, positions.side, DIVIDEND/BORROW exit layers, BORROW_LOST event');

COMMIT;
