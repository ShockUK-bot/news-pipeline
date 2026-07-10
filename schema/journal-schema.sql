-- ============================================================================
-- Multi-Agent News Trading System — Journal / Decision-Log Schema
-- Version: 1.0   (baseline v0.5, July 2026)
-- Target:  PostgreSQL 15+  (TimescaleDB optional; see companion spec §7)
--
-- The journal is the system's institutional memory and the one irreplaceable
-- asset (baseline §11.4). Consumers: A7 EOD report, A8 briefing, A9 weekend
-- review, A11 eval, A12 guard context, C6 dashboard, C9 replay verification.
--
-- Conventions (companion spec §2):
--   * All timestamps TIMESTAMPTZ, stored UTC. ET conversion in code only.
--   * Every table carries schema_version (baseline §11.5).
--   * config_version = git commit SHA of the config repo active at write time.
--   * R = initial risk unit (entry price − initial stop) per position.
--   * Money in NUMERIC(14,4); never floats.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS journal;
SET search_path TO journal;

-- ----------------------------------------------------------------------------
-- 0. Schema metadata & config registry
-- ----------------------------------------------------------------------------

CREATE TABLE schema_meta (
  schema_version  SMALLINT PRIMARY KEY,
  applied_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  description     TEXT NOT NULL
);
INSERT INTO schema_meta VALUES (1, now(), 'Initial journal schema, baseline v0.5');

CREATE TABLE config_versions (
  config_version  TEXT PRIMARY KEY,          -- git commit SHA (config repo)
  applied_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  summary         TEXT,                      -- commit subject line
  proposal_id     BIGINT,                    -- A9 proposal that produced it (FK added below)
  schema_version  SMALLINT NOT NULL DEFAULT 1
);

-- ----------------------------------------------------------------------------
-- 1. Regime snapshots (C8) — referenced by every decision
-- ----------------------------------------------------------------------------

CREATE TABLE regime_snapshots (
  regime_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts              TIMESTAMPTZ NOT NULL,
  features        JSONB NOT NULL,            -- {index_trend, vix, vix_chg, breadth, sector_rs, ...}
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_regime_ts ON regime_snapshots (ts DESC);

-- ----------------------------------------------------------------------------
-- 2. decisions — the spine. One row per stage verdict, including every veto
--    and discard (baseline principle 5: everything is logged).
-- ----------------------------------------------------------------------------

CREATE TABLE decisions (
  decision_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- What is being decided about --------------------------------------------
  signal_id       TEXT NOT NULL,             -- unit flowing through the pipeline
  item_id         TEXT,                      -- news-store item (NULL for synthetic/scheduled)
  item_revision   SMALLINT,                  -- which revision was seen (corrections, v0.4)
  derived_from    BIGINT REFERENCES decisions(decision_id),  -- sympathy-lane parent (v0.2)
  ticker          TEXT,                      -- NULL for untagged/macro items

  -- Who decided and how ------------------------------------------------------
  stage           TEXT NOT NULL CHECK (stage IN
                    ('TRIAGE','ANALYST','GATE','RISK','ORDER','GUARD',
                     'PREMARKET','POSITION_REVIEW','SYSTEM')),
  agent           TEXT NOT NULL,             -- 'A1'..'A12','C3','C4','C7'
  action          TEXT NOT NULL,             -- ESCALATE|DISCARD|THESIS|REJECT|PASS|VETO|
                                             -- SIZED|SUBMITTED|FILLED|EXIT|HOLD|TIGHTEN_STOP|...
  veto_reason     TEXT,                      -- machine code when action='VETO'
                                             -- (GATE_NO_CONFIRM, GATE_EXTENDED, CREDIBILITY,
                                             --  SIZE_CLIPPED, HEAT_CAP, LIQUIDITY, HALTED,
                                             --  KILL_SWITCH, BREAKER, TRADES_PER_DAY, ...)

  -- Full model/gate output ---------------------------------------------------
  payload         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- entire structured output
                                             -- (A2 thesis incl. expected_move_window,
                                             --  invalidations, source_risk; C3 gate numbers;
                                             --  A3 sizing math; A12 verdict)
  reason          TEXT,                      -- human-readable reasoning snippet (tape display)
  confidence      REAL,                      -- ordinal (baseline rule 6)

  -- Provenance (replay + attribution) ---------------------------------------
  model_id        TEXT,                      -- e.g. 'qwen3-32b-q5', NULL for pure code
  latency_ms      INTEGER,
  config_version  TEXT NOT NULL REFERENCES config_versions(config_version),
  regime_id       BIGINT REFERENCES regime_snapshots(regime_id),
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_dec_ts        ON decisions (ts DESC);
CREATE INDEX idx_dec_signal    ON decisions (signal_id, ts);
CREATE INDEX idx_dec_item      ON decisions (item_id) WHERE item_id IS NOT NULL;
CREATE INDEX idx_dec_ticker_ts ON decisions (ticker, ts DESC) WHERE ticker IS NOT NULL;
CREATE INDEX idx_dec_veto      ON decisions (ts DESC) WHERE action = 'VETO';
CREATE INDEX idx_dec_stage     ON decisions (stage, ts DESC);

-- ----------------------------------------------------------------------------
-- 3. intents & orders & fills — A3 output through C4's order state machine
-- ----------------------------------------------------------------------------

CREATE TABLE intents (
  intent_id       TEXT PRIMARY KEY,          -- idempotency key (v0.4): duplicates are no-ops
  decision_id     BIGINT NOT NULL REFERENCES decisions(decision_id),
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  ticker          TEXT NOT NULL,
  side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),  -- SELL = exits only (long-only)
  qty             INTEGER NOT NULL CHECK (qty > 0),
  limit_price     NUMERIC(14,4) NOT NULL,    -- limit orders only (rule 11)
  gate_snapshot   JSONB,                     -- C3 price/volume snapshot the limit was priced off
  exit_policy     JSONB,                     -- full v0.3 exit_policy object (entries)
  horizon         TEXT CHECK (horizon IN ('SHORT','LONG')),
  effective_capital NUMERIC(14,4),           -- min(broker_equity, trading_capital) at sizing (v0.5)
  risk_budget     NUMERIC(14,4),             -- $ risked = risk_per_trade_pct * effective_capital
  status          TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING','SUBMITTED','REJECTED','FILLED',
                                      'PARTIAL','CANCELLED','EXPIRED')),
  config_version  TEXT NOT NULL REFERENCES config_versions(config_version),
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_intents_ticker ON intents (ticker, ts DESC);

CREATE TABLE orders (
  order_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  intent_id       TEXT REFERENCES intents(intent_id),     -- NULL for broker-side catastrophe stops
  position_id     BIGINT,                                  -- FK added after positions
  broker_order_id TEXT UNIQUE,
  order_role      TEXT NOT NULL CHECK (order_role IN
                    ('ENTRY','EXIT','CATASTROPHE_STOP','SCALE_OUT','FLATTEN')),
  state           TEXT NOT NULL CHECK (state IN
                    ('NEW','ACCEPTED','PARTIAL','FILLED','CANCELLED','REJECTED','EXPIRED','HELD_HALT')),
  qty             INTEGER NOT NULL,
  limit_price     NUMERIC(14,4),
  stop_price      NUMERIC(14,4),
  submitted_ts    TIMESTAMPTZ,
  closed_ts       TIMESTAMPTZ,
  raw             JSONB,                     -- last broker payload (reconciliation evidence)
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_orders_intent ON orders (intent_id);

CREATE TABLE fills (
  fill_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id        BIGINT NOT NULL REFERENCES orders(order_id),
  ts              TIMESTAMPTZ NOT NULL,
  qty             INTEGER NOT NULL,
  price           NUMERIC(14,4) NOT NULL,
  fees            NUMERIC(14,4) NOT NULL DEFAULT 0,
  broker_exec_id  TEXT UNIQUE,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_fills_order ON fills (order_id);

-- ----------------------------------------------------------------------------
-- 4. positions & the exit-policy state machine (v0.3 §5)
-- ----------------------------------------------------------------------------

CREATE TABLE positions (
  position_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ticker          TEXT NOT NULL,
  horizon         TEXT NOT NULL CHECK (horizon IN ('SHORT','LONG')),
  profile         TEXT NOT NULL,             -- 'short_term_v1' | 'long_term_v1' | ...
  status          TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')),

  opened_ts       TIMESTAMPTZ NOT NULL,
  closed_ts       TIMESTAMPTZ,

  entry_intent_id TEXT NOT NULL REFERENCES intents(intent_id),
  thesis_decision_id BIGINT NOT NULL REFERENCES decisions(decision_id),  -- the A2 thesis
  item_id         TEXT,                      -- originating news item (dashboard drill-down)

  qty_initial     INTEGER NOT NULL,
  qty_open        INTEGER NOT NULL,          -- decremented by scale-outs
  avg_entry       NUMERIC(14,4) NOT NULL,
  initial_stop    NUMERIC(14,4) NOT NULL,    -- defines R: r_unit = avg_entry - initial_stop
  r_unit          NUMERIC(14,4) NOT NULL CHECK (r_unit > 0),

  exit_policy     JSONB NOT NULL,            -- CURRENT policy state (stops move; history below)
  catastrophe_stop_order_id BIGINT REFERENCES orders(order_id),  -- broker-resident tier (v0.4)

  -- C4 mark-to-market cache (dashboard reads; refreshed on bar close) --------
  last_price      NUMERIC(14,4),
  last_price_ts   TIMESTAMPTZ,

  realized_pnl    NUMERIC(14,4) NOT NULL DEFAULT 0,   -- accumulated over partial exits
  config_version  TEXT NOT NULL REFERENCES config_versions(config_version),
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_pos_status ON positions (status, opened_ts DESC);
CREATE INDEX idx_pos_ticker ON positions (ticker) WHERE status = 'OPEN';
ALTER TABLE orders ADD CONSTRAINT fk_orders_position
  FOREIGN KEY (position_id) REFERENCES positions(position_id);

-- Exit-policy state history: every mutation of every exit layer, forever.
CREATE TABLE position_events (
  event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  position_id     BIGINT NOT NULL REFERENCES positions(position_id),
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  event_type      TEXT NOT NULL CHECK (event_type IN
                    ('STOPS_PLACED','BREAKEVEN_MOVED','TRAIL_UPDATED','STOP_TIGHTENED',
                     'TIME_STOP_ARMED','INVALIDATION_ARMED','INVALIDATION_FIRED',
                     'EARNINGS_BLACKOUT_FLAGGED','OVERNIGHT_HOLD_DECISION',
                     'HALT_FROZEN','HALT_RESUMED','SCALE_OUT','EXIT','GUARD_ACTION',
                     'CORPORATE_ACTION_ADJ','RECONCILED')),
  actor           TEXT NOT NULL,             -- 'C4','A12','A6','OPERATOR','BROKER'
  old_value       JSONB,
  new_value       JSONB,
  r_progress      NUMERIC(8,3),              -- unrealized R at event time
  detail          TEXT,
  decision_id     BIGINT REFERENCES decisions(decision_id),  -- model decision that caused it
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_pev_position ON position_events (position_id, ts);

-- Per-exit attribution: one row per exit execution, INCLUDING partials (v0.3 L4).
CREATE TABLE exits (
  exit_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  position_id     BIGINT NOT NULL REFERENCES positions(position_id),
  order_id        BIGINT REFERENCES orders(order_id),
  ts              TIMESTAMPTZ NOT NULL,
  exit_layer      TEXT NOT NULL CHECK (exit_layer IN
                    ('STOP','CATASTROPHE','BREAKEVEN','TRAIL','TIME','TARGET',
                     'INVALIDATION','GUARD','REVIEW','EARNINGS','OVERNIGHT',
                     'BREAKER','KILL','OPERATOR')),
  qty             INTEGER NOT NULL,
  price           NUMERIC(14,4) NOT NULL,
  realized_pnl    NUMERIC(14,4) NOT NULL,
  r_multiple      NUMERIC(8,3) NOT NULL,     -- realized_pnl / (r_unit * qty)
  is_partial      BOOLEAN NOT NULL DEFAULT FALSE,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_exits_position ON exits (position_id);
CREATE INDEX idx_exits_layer_ts ON exits (exit_layer, ts DESC);

-- ----------------------------------------------------------------------------
-- 5. Guard ledger (A12) — verdicts now, outcome classification later (A11)
-- ----------------------------------------------------------------------------

CREATE TABLE guard_ledger (
  guard_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  decision_id     BIGINT NOT NULL REFERENCES decisions(decision_id),
  position_id     BIGINT NOT NULL REFERENCES positions(position_id),
  item_id         TEXT NOT NULL,
  ts              TIMESTAMPTZ NOT NULL,
  thesis_intact   BOOLEAN NOT NULL,
  recommended_action TEXT NOT NULL CHECK (recommended_action IN ('HOLD','TIGHTEN_STOP','EXIT')),
  urgency         TEXT,
  auto_executed   BOOLEAN NOT NULL DEFAULT FALSE,       -- config-gated (rule 12)
  action_taken    TEXT,                                  -- what actually happened
  outcome_class   TEXT CHECK (outcome_class IN ('SAVE','SHAKEOUT','NEUTRAL')),  -- A11, later
  outcome_pnl_r   NUMERIC(8,3),                          -- counterfactual delta in R
  classified_ts   TIMESTAMPTZ,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_guard_position ON guard_ledger (position_id);
CREATE INDEX idx_guard_pending  ON guard_ledger (ts) WHERE outcome_class IS NULL;

-- ----------------------------------------------------------------------------
-- 6. A11 measurement layer (v0.3 exit metrics + counterfactuals)
-- ----------------------------------------------------------------------------

CREATE TABLE trade_metrics (            -- one row per CLOSED position, written by A11 nightly
  position_id     BIGINT PRIMARY KEY REFERENCES positions(position_id),
  computed_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
  holding_seconds BIGINT NOT NULL,
  mae_r           NUMERIC(8,3) NOT NULL,     -- max adverse excursion, in R
  mfe_r           NUMERIC(8,3) NOT NULL,     -- max favorable excursion, in R
  realized_r      NUMERIC(8,3) NOT NULL,
  exit_efficiency NUMERIC(6,4),              -- realized_pnl / MFE$ (NULL if MFE<=0)
  magnitude_predicted NUMERIC(8,4),          -- from A2 thesis payload
  magnitude_realized  NUMERIC(8,4),
  window_hit      BOOLEAN,                   -- reached min progress inside expected_move_window?
  schema_version  SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE counterfactuals (          -- post-exit and post-veto price paths, recorded by code
  cf_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN ('POST_EXIT','VETOED_TRADE','GUARD_CF')),
  exit_id         BIGINT REFERENCES exits(exit_id),
  decision_id     BIGINT REFERENCES decisions(decision_id),   -- the veto, for VETOED_TRADE
  ticker          TEXT NOT NULL,
  anchor_ts       TIMESTAMPTZ NOT NULL,      -- exit time / hypothetical entry time
  anchor_price    NUMERIC(14,4) NOT NULL,
  horizon_desc    TEXT NOT NULL,             -- e.g. '1R_equivalent', '2_sessions'
  path            JSONB NOT NULL,            -- [[ts,price],...] downsampled
  outcome_r       NUMERIC(8,3),              -- foregone/avoided result in R terms
  computed_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  CHECK (exit_id IS NOT NULL OR decision_id IS NOT NULL)
);
CREATE INDEX idx_cf_kind ON counterfactuals (kind, anchor_ts DESC);

CREATE TABLE metric_rollups (           -- A11 nightly/weekly aggregates that A9 and A7 read
  rollup_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  period_start    DATE NOT NULL,
  granularity     TEXT NOT NULL CHECK (granularity IN ('DAY','WEEK')),
  metric          TEXT NOT NULL,             -- 'triage_escalation_rate','gate_pass_rate',
                                             -- 'exit_efficiency:TRAIL','guard_save_rate',
                                             -- 'veto_counterfactual_pnl_r', ...
  value           NUMERIC(16,6),
  breakdown       JSONB,                     -- per-ticker/-profile/-regime slices
  config_version  TEXT REFERENCES config_versions(config_version),
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (period_start, granularity, metric)
);

-- ----------------------------------------------------------------------------
-- 7. Governance: A9 proposals with their attribution loop (baseline §8)
-- ----------------------------------------------------------------------------

CREATE TABLE proposals (
  proposal_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  author          TEXT NOT NULL DEFAULT 'A9',
  title           TEXT NOT NULL,
  current_state   TEXT NOT NULL,             -- parameter/prompt as-is
  proposed_diff   TEXT NOT NULL,
  evidence        JSONB NOT NULL,            -- {decision_ids:[], rollup_ids:[], n_instances:int}
  expected_effect TEXT NOT NULL,
  success_metric  TEXT NOT NULL,             -- metric name + target, evaluated next weekend
  status          TEXT NOT NULL DEFAULT 'PROPOSED'
                    CHECK (status IN ('PROPOSED','APPROVED','REJECTED','SHADOW','EVALUATED')),
  reviewed_ts     TIMESTAMPTZ,
  config_version_result TEXT REFERENCES config_versions(config_version),  -- commit if approved
  evaluation      JSONB,                     -- next weekend's verdict vs success_metric
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
ALTER TABLE config_versions ADD CONSTRAINT fk_cfg_proposal
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id);

-- ----------------------------------------------------------------------------
-- 8. Operational controls, audit, health, outbox (v0.5 / C5 / C6 / C7)
-- ----------------------------------------------------------------------------

CREATE TABLE control (
  key             TEXT PRIMARY KEY,          -- 'kill_switch','drawdown_breaker','trading_capital'
  value           TEXT NOT NULL,
  updated_ts      TIMESTAMPTZ,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
INSERT INTO control (key, value) VALUES
  ('kill_switch','0'), ('drawdown_breaker','0'), ('trading_capital','50000');

CREATE TABLE audit (
  audit_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor           TEXT NOT NULL,             -- dashboard user, 'C4', 'C7'
  action          TEXT NOT NULL,             -- KILL_SWITCH_ON/OFF, CAPITAL_SET, BREAKER_TRIP, ...
  old_value       TEXT,
  new_value       TEXT,
  detail          TEXT,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_audit_ts ON audit (ts DESC);

CREATE TABLE health (
  component       TEXT PRIMARY KEY,          -- 'ingestion','triage_model','analyst_model',
                                             -- 'broker_api','scheduler','backup'
  status          TEXT NOT NULL CHECK (status IN ('OK','DEGRADED','DOWN')),
  detail          TEXT,
  updated_ts      TIMESTAMPTZ NOT NULL,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE outbox (                    -- A7/A8 write; C5 mailer sends (no agent has SMTP)
  message_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind            TEXT NOT NULL CHECK (kind IN ('EOD_REPORT','MORNING_BRIEFING','ALERT')),
  subject         TEXT NOT NULL,
  body            TEXT NOT NULL,             -- rendered; numbers computed by code (rule 5)
  fact_sheet      JSONB,                     -- the code-computed numbers the narrative was given
  status          TEXT NOT NULL DEFAULT 'QUEUED'
                    CHECK (status IN ('QUEUED','SENT','FAILED')),
  sent_ts         TIMESTAMPTZ,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_outbox_queued ON outbox (created_ts) WHERE status = 'QUEUED';

-- ============================================================================
-- 9. C6 dashboard views — the exact read shapes from c6-dashboard-spec v1.2 §6.
--    The reference implementation binds to these names/columns unchanged.
-- ============================================================================

CREATE VIEW dash_decisions AS
SELECT decision_id                              AS id,
       EXTRACT(EPOCH FROM ts)                   AS ts,
       item_id,
       stage,
       ticker,
       action,
       COALESCE(reason, veto_reason)            AS detail,
       latency_ms
FROM decisions
ORDER BY decision_id DESC;

CREATE VIEW dash_positions AS
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
       (SELECT e.exit_layer FROM exits e WHERE e.position_id = p.position_id
        ORDER BY e.ts DESC LIMIT 1)             AS exit_reason,
       p.realized_pnl,
       LEFT(d.reason, 200)                      AS thesis,
       p.item_id
FROM positions p
JOIN decisions d ON d.decision_id = p.thesis_decision_id;

CREATE VIEW dash_health  AS SELECT component, status, detail,
       EXTRACT(EPOCH FROM updated_ts) AS updated_ts FROM health;
CREATE VIEW dash_control AS SELECT key, value,
       EXTRACT(EPOCH FROM updated_ts) AS updated_ts FROM control;
CREATE VIEW dash_audit   AS SELECT audit_id AS id, EXTRACT(EPOCH FROM ts) AS ts,
       actor, action, COALESCE(old_value||' -> '||new_value, detail) AS detail FROM audit;

-- ============================================================================
-- End of schema v1. Migration policy: additive changes bump schema_version
-- DEFAULT and append to schema_meta; destructive changes forbidden (A9 must
-- read last month's rows — baseline §11.5).
-- ============================================================================

