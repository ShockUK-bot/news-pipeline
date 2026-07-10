-- ============================================================================
-- Multi-Agent News Trading System — News Store + Queue Schema
-- Version: 1.0   (baseline v0.5, July 2026)
-- Target:  PostgreSQL 15+
--
-- Phase 1 artifact. Three concerns in one schema:
--   news.*   — normalized news items (revisable), dedup clusters, quarantine,
--              symbol lifecycle, ingestion gap log
--   queue.*  — the Postgres-backed message queues connecting pipeline stages
--              (at-least-once, SKIP LOCKED, dedup on dedup_key — rule 19)
--
-- Companion: queue-contracts-spec.md (message JSON contracts per hop).
-- Conventions match the journal schema: TIMESTAMPTZ/UTC, schema_version
-- everywhere, TEXT + CHECK instead of enums, money/none here.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS news;
CREATE SCHEMA IF NOT EXISTS queue;

-- ============================================================================
-- NEWS SCHEMA
-- ============================================================================
SET search_path TO news;

-- ----------------------------------------------------------------------------
-- 1. news_items — the normalized item, REVISABLE (v0.4 corrections).
--    Composite PK (item_id, revision): a correction is a new row, same item_id.
-- ----------------------------------------------------------------------------
CREATE TABLE news_items (
  item_id         TEXT NOT NULL,             -- source-scoped stable id (e.g. 'alpaca:40892639')
  revision        SMALLINT NOT NULL DEFAULT 1,
  is_correction   BOOLEAN NOT NULL DEFAULT FALSE,
  supersedes      SMALLINT,                  -- revision this one corrects (NULL for rev 1)

  -- Source & trust -----------------------------------------------------------
  source          TEXT NOT NULL,             -- 'alpaca_benzinga','polygon','edgar','rss:<feed>'
  source_tier     SMALLINT NOT NULL CHECK (source_tier IN (1,2,3)),  -- v0.2 trust tiers
  source_url      TEXT,
  author          TEXT,

  -- Content -------------------------------------------------------------------
  headline        TEXT NOT NULL,
  summary         TEXT,
  content_hash    TEXT NOT NULL,             -- sha256 of normalized headline+summary+body
  raw             JSONB,                     -- original payload (hot tier; demoted per §11.3)
  body_ref        TEXT,                      -- object-store key once demoted (raw set NULL)

  -- Symbols (OPTIONAL by design — v0.2; A1 infers for untagged items) ---------
  symbols         TEXT[] NOT NULL DEFAULT '{}',
  channels        TEXT[] NOT NULL DEFAULT '{}',   -- feed-provided tags ('earnings','m&a',...)
  lang            TEXT NOT NULL DEFAULT 'en',

  -- Time discipline (§11.5 — both clocks are load-bearing) --------------------
  published_ts    TIMESTAMPTZ NOT NULL,      -- the SOURCE's claimed publication time
  received_ts     TIMESTAMPTZ NOT NULL,      -- OUR wall clock at ingestion
                                             -- (replay ordering + lookahead-bias guard:
                                             --  nothing may act on an item before received_ts)
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  PRIMARY KEY (item_id, revision)
);
CREATE INDEX idx_items_received ON news_items (received_ts DESC);
CREATE INDEX idx_items_symbols  ON news_items USING GIN (symbols);
CREATE INDEX idx_items_hash     ON news_items (content_hash);
CREATE INDEX idx_items_source   ON news_items (source, received_ts DESC);

-- Latest revision per item (what most readers want)
CREATE VIEW news_items_latest AS
SELECT DISTINCT ON (item_id) *
FROM news_items
ORDER BY item_id, revision DESC;

-- ----------------------------------------------------------------------------
-- 2. clusters — C2's story grouping. Embeddings live in the vector store;
--    Postgres holds membership + the corroboration count C3 consumes (v0.2).
-- ----------------------------------------------------------------------------
CREATE TABLE clusters (
  cluster_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
  canonical_item  TEXT NOT NULL,             -- first item_id seen for the story
  schema_version  SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE cluster_members (
  cluster_id      BIGINT NOT NULL REFERENCES clusters(cluster_id),
  item_id         TEXT NOT NULL,
  revision        SMALLINT NOT NULL,
  source          TEXT NOT NULL,             -- denormalized for the outlet count
  similarity      REAL,                      -- cosine sim to canonical at admission
  added_ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  PRIMARY KEY (cluster_id, item_id, revision),
  FOREIGN KEY (item_id, revision) REFERENCES news_items(item_id, revision)
);
CREATE INDEX idx_cm_item ON cluster_members (item_id);

-- Corroboration = count of INDEPENDENT outlets in the cluster (C3 credibility rule)
CREATE VIEW cluster_corroboration AS
SELECT cluster_id,
       COUNT(DISTINCT source)                    AS independent_outlets,
       COUNT(*)                                  AS total_items,
       MIN(added_ts)                             AS first_seen,
       MAX(added_ts)                             AS last_seen
FROM cluster_members
GROUP BY cluster_id;

-- ----------------------------------------------------------------------------
-- 3. quarantine — malformed input is kept, never dropped (v0.4).
--    C7 alerts on rate spikes; that is how a silently changed feed is found.
-- ----------------------------------------------------------------------------
CREATE TABLE quarantine (
  quarantine_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  received_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
  source          TEXT NOT NULL,
  reason_code     TEXT NOT NULL CHECK (reason_code IN
                    ('UNPARSEABLE_JSON','BAD_TIMESTAMP','MISSING_REQUIRED_FIELD',
                     'UNKNOWN_SCHEMA','OVERSIZE','DUPLICATE_CONFLICT','SYMBOL_UNKNOWN',
                     'ENCODING_ERROR','OTHER')),
  detail          TEXT,
  raw             JSONB,                     -- best-effort capture; TEXT dump if not JSON
  raw_text        TEXT,
  reviewed        BOOLEAN NOT NULL DEFAULT FALSE,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_quarantine_ts ON quarantine (received_ts DESC) WHERE NOT reviewed;

-- ----------------------------------------------------------------------------
-- 4. symbol_map + corporate_actions — ticker lifecycle (v0.4).
--    Effective-dated: joins pick the mapping valid at event time.
-- ----------------------------------------------------------------------------
CREATE TABLE symbol_map (
  map_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  symbol          TEXT NOT NULL,
  entity_id       TEXT NOT NULL,             -- stable company identifier (CIK preferred)
  entity_name     TEXT NOT NULL,
  effective_from  DATE NOT NULL,
  effective_to    DATE,                      -- NULL = current
  reason          TEXT NOT NULL DEFAULT 'LISTING'
                    CHECK (reason IN ('LISTING','RENAME','MERGER','SPINOFF','DELISTING')),
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_symmap_symbol ON symbol_map (symbol, effective_from);
CREATE INDEX idx_symmap_entity ON symbol_map (entity_id);

CREATE TABLE corporate_actions (
  action_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  symbol          TEXT NOT NULL,
  action_type     TEXT NOT NULL CHECK (action_type IN
                    ('SPLIT','REVERSE_SPLIT','SYMBOL_CHANGE','DIVIDEND_SPECIAL',
                     'MERGER','DELISTING')),
  ex_date         DATE NOT NULL,
  ratio           NUMERIC(12,6),             -- e.g. 10 for 10:1 split
  new_symbol      TEXT,                      -- for SYMBOL_CHANGE / MERGER
  raw             JSONB,
  applied_positions BOOLEAN NOT NULL DEFAULT FALSE,  -- C4 adjusted position state?
  applied_bars      BOOLEAN NOT NULL DEFAULT FALSE,  -- bar store adjusted?
  schema_version  SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_ca_exdate ON corporate_actions (ex_date DESC);

-- ----------------------------------------------------------------------------
-- 5. ingestion_gaps — explicit gap log (C1 reliability; surfaced to A4/A8)
-- ----------------------------------------------------------------------------
CREATE TABLE ingestion_gaps (
  gap_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source          TEXT NOT NULL,
  gap_start       TIMESTAMPTZ NOT NULL,
  gap_end         TIMESTAMPTZ,               -- NULL while ongoing
  detected_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
  detail          TEXT,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);

-- ============================================================================
-- QUEUE SCHEMA — Postgres-backed queues (single-host; one fewer moving part
-- than Redis; LISTEN/NOTIFY wakes consumers; SKIP LOCKED makes claims safe).
-- Semantics: at-least-once delivery + consumer dedup on dedup_key (rule 19).
-- ============================================================================
SET search_path TO queue;

CREATE TABLE messages (
  msg_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  queue_name      TEXT NOT NULL,             -- see queue-contracts-spec §2 for the registry
  dedup_key       TEXT NOT NULL,             -- e.g. 'item-777:2' — consumer-side idempotency
  priority        SMALLINT NOT NULL DEFAULT 100,   -- lower = sooner (A12 path uses 0)
  payload         JSONB NOT NULL,            -- the contract body (spec §3)
  schema_version  SMALLINT NOT NULL DEFAULT 1,

  enqueued_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
  available_ts    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- delayed delivery (open handoff at 9:30)
  claimed_by      TEXT,
  claimed_ts      TIMESTAMPTZ,
  done_ts         TIMESTAMPTZ,
  attempts        SMALLINT NOT NULL DEFAULT 0,
  max_attempts    SMALLINT NOT NULL DEFAULT 5,
  last_error      TEXT,
  UNIQUE (queue_name, dedup_key)             -- duplicate enqueue = no-op (ON CONFLICT DO NOTHING)
);
CREATE INDEX idx_q_ready ON messages (queue_name, priority, available_ts)
  WHERE done_ts IS NULL AND claimed_ts IS NULL;
CREATE INDEX idx_q_claimed ON messages (queue_name, claimed_ts)
  WHERE done_ts IS NULL AND claimed_ts IS NOT NULL;

-- Claim the next ready message (safe under concurrency via SKIP LOCKED).
CREATE OR REPLACE FUNCTION claim_next(p_queue TEXT, p_consumer TEXT)
RETURNS SETOF messages LANGUAGE plpgsql AS $$
BEGIN
  RETURN QUERY
  UPDATE messages m
  SET claimed_by = p_consumer, claimed_ts = now(), attempts = m.attempts + 1
  WHERE m.msg_id = (
    SELECT msg_id FROM messages
    WHERE queue_name = p_queue AND done_ts IS NULL AND claimed_ts IS NULL
      AND available_ts <= now()
    ORDER BY priority, available_ts
    LIMIT 1
    FOR UPDATE SKIP LOCKED
  )
  RETURNING m.*;
END $$;

-- Ack / fail helpers. Failure past max_attempts routes to news.quarantine
-- (the pipeline's dead-letter destination) and marks the message done.
CREATE OR REPLACE FUNCTION ack(p_msg_id BIGINT)
RETURNS void LANGUAGE sql AS
$$ UPDATE messages SET done_ts = now() WHERE msg_id = p_msg_id $$;

CREATE OR REPLACE FUNCTION fail(p_msg_id BIGINT, p_error TEXT)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE m messages;
BEGIN
  SELECT * INTO m FROM messages WHERE msg_id = p_msg_id;
  IF m.attempts >= m.max_attempts THEN
    INSERT INTO news.quarantine (source, reason_code, detail, raw)
    VALUES ('queue:' || m.queue_name, 'OTHER',
            'DLQ after ' || m.attempts || ' attempts: ' || p_error, m.payload);
    UPDATE messages SET done_ts = now(), last_error = p_error WHERE msg_id = p_msg_id;
  ELSE
    UPDATE messages
    SET claimed_by = NULL, claimed_ts = NULL, last_error = p_error,
        available_ts = now() + (interval '5 seconds' * attempts)   -- linear backoff
    WHERE msg_id = p_msg_id;
  END IF;
END $$;

-- Reaper: reclaim messages whose consumer died mid-claim (C7 runs periodically).
CREATE OR REPLACE FUNCTION reap_stale(p_queue TEXT, p_timeout INTERVAL)
RETURNS INTEGER LANGUAGE sql AS $$
  WITH r AS (
    UPDATE messages SET claimed_by = NULL, claimed_ts = NULL
    WHERE queue_name = p_queue AND done_ts IS NULL
      AND claimed_ts IS NOT NULL AND claimed_ts < now() - p_timeout
    RETURNING 1)
  SELECT COALESCE(count(*), 0)::integer FROM r
$$;

-- ============================================================================
-- End. Migration policy identical to the journal schema: additive only.
-- ============================================================================

