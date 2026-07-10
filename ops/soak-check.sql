-- soak-check.sql v1.0 — morning triage for the Phase 4 paper soak.
-- Usage: psql "$PIPELINE_DSN" -f ops/soak-check.sql
-- Every section should be boring. Anything surprising goes in the soak log.

\echo '=== 1. component health (all OK? updated recently?) ==='
SELECT component, status,
       CASE WHEN updated_ts < now() - interval '15 minutes'
            THEN 'STALE ' || to_char(now()-updated_ts, 'HH24:MI') ELSE 'fresh' END AS freshness,
       left(detail, 60) AS detail, updated_ts
FROM journal.health ORDER BY (status <> 'OK') DESC, component;

\echo '=== 2. backup freshness (must be < 26h old) ==='
SELECT status, detail, updated_ts, now()-updated_ts AS age,
       CASE WHEN updated_ts < now() - interval '26 hours' THEN '*** BACKUP STALE ***' ELSE 'ok' END AS verdict
FROM journal.health WHERE component='backup';

\echo '=== 3. queue depths (pending, oldest age, dead-lettered) ==='
SELECT queue_name,
       count(*) FILTER (WHERE done_ts IS NULL AND attempts < max_attempts) AS pending,
       to_char(now() - min(enqueued_ts) FILTER (WHERE done_ts IS NULL AND attempts < max_attempts), 'HH24:MI:SS') AS oldest_pending,
       count(*) FILTER (WHERE done_ts IS NULL AND attempts >= max_attempts) AS dead
FROM queue.messages GROUP BY 1 ORDER BY 1;

\echo '=== 4. decisions last 24h by stage/action ==='
SELECT stage, action, count(*), round(avg(latency_ms)) AS avg_ms
FROM journal.decisions WHERE ts > now() - interval '24 hours'
GROUP BY 1,2 ORDER BY 1,2;

\echo '=== 5. REJECTs last 24h (rising rate = prompt/grammar problem) ==='
SELECT stage, model_id, count(*), left(min(reason),80) AS sample_reason
FROM journal.decisions
WHERE ts > now() - interval '24 hours' AND action = 'REJECT'
GROUP BY 1,2 ORDER BY 3 DESC LIMIT 10;

\echo '=== 6. quarantine awaiting review ==='
SELECT quarantine_id, received_ts, source, reason_code, left(detail,60) AS detail
FROM news.quarantine WHERE NOT reviewed ORDER BY received_ts DESC LIMIT 20;

\echo '=== 7. ingestion gaps last 24h (gap_end NULL = STILL OPEN) ==='
SELECT gap_id, source, gap_start, gap_end,
       CASE WHEN gap_end IS NULL THEN '*** ONGOING ***' ELSE to_char(gap_end-gap_start,'HH24:MI:SS') END AS duration,
       left(detail,50) AS detail
FROM news.ingestion_gaps WHERE gap_start > now() - interval '24 hours'
ORDER BY gap_start DESC;

\echo '=== 8. control flags ==='
SELECT key, value, updated_ts FROM journal.control ORDER BY key;

\echo '=== 9. open positions (catastrophe stop MUST be present) ==='
SELECT position_id, ticker, horizon, qty_open, avg_entry, initial_stop, last_price,
       CASE WHEN catastrophe_stop_order_id IS NULL THEN '*** NO CAT STOP ***' ELSE 'ok' END AS cat_stop,
       opened_ts
FROM journal.positions WHERE status='OPEN' ORDER BY opened_ts;

\echo '=== 10. orders by state (non-terminal only) ==='
SELECT order_role, state, count(*)
FROM journal.orders WHERE closed_ts IS NULL GROUP BY 1,2 ORDER BY 1,2;

\echo '=== 11. trades today vs limit ==='
SELECT (SELECT count(*) FROM journal.positions WHERE opened_ts::date = current_date) AS opened_today,
       (SELECT value FROM journal.control WHERE key='max_trades_per_day') AS max_per_day;
