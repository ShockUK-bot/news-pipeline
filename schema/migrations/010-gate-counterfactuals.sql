-- Migration 010 — journal.gate_counterfactuals (v0.12.10, 2026-08-03)
--
-- WHY: the gate went 0-for-21 on the first fully-healthy session
-- (2026-08-03) against placeholder thresholds, and nothing measured what
-- those vetoes cost or saved — the §14 gate-threshold design item has no
-- data to design FROM. This table records every FINAL gate veto and, after
-- the session closes, what the stock actually did (30 min / 2 h / close /
-- max favorable and adverse excursion from the veto price). C3 fills it;
-- nothing else writes here. Additive only.
--
-- (Per the 009 lesson: this migration rebuilds no constraints and touches
-- no existing objects.)

BEGIN;

CREATE TABLE journal.gate_counterfactuals (
    cf_id             bigserial PRIMARY KEY,
    decision_id       bigint NOT NULL
                        REFERENCES journal.decisions (decision_id),
    signal_id         text NOT NULL,
    item_id           text,
    ticker            text NOT NULL,
    direction         text NOT NULL DEFAULT 'up',
    rule              text NOT NULL,          -- intraday | open_handoff | scanner
    veto_reason       text NOT NULL,
    veto_ts           timestamptz NOT NULL,
    price_at_veto     numeric,
    prenews_price     numeric,                -- detect price on scanner rows
    pct_move_at_veto  numeric,
    vol_mult_at_veto  numeric,
    -- filled by the post-close sweep:
    price_30m         numeric,
    price_2h          numeric,
    price_eod         numeric,
    max_up_pct        numeric,                -- best long case from veto price
    max_down_pct      numeric,                -- best short case (negative)
    complete          boolean NOT NULL DEFAULT false,
    fill_note         text,
    created_ts        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX gate_cf_incomplete_idx
    ON journal.gate_counterfactuals (veto_ts) WHERE NOT complete;
CREATE INDEX gate_cf_reason_idx
    ON journal.gate_counterfactuals (veto_reason, veto_ts);

INSERT INTO journal.schema_meta VALUES
  (10, now(),
   'gate_counterfactuals: post-veto outcome tracking for gate-threshold tuning (v0.12.10)');

COMMIT;
