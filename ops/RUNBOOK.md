# Pipeline Runbook (Phase 4, D5)

Operator procedures for the failure modes that matter. Every scenario ends
with "how you know it worked." Commands assume the Spark, repo at
`~/pipeline`, Postgres 16 local, services under systemd. **The broker is the
source of truth; the journal is the record; positions are protected by
broker-resident catastrophe stops even when everything here is down.**

Environment used throughout:
```bash
cd ~/pipeline
export PYTHONPATH=src PIPELINE_DSN=postgresql://trader:trader_dev@127.0.0.1:5432/trading
PSQL="psql $PIPELINE_DSN"
```

---

## 1. Total Spark failure (box dead / unbootable)

Positions are safe: catastrophe stops are GTC at the broker and do not
depend on the Spark. **Do not panic-flatten from your phone.**

1. Confirm protection from any browser: Alpaca dashboard → Orders → open
   stop orders should match one per open position.
2. If you must reduce risk manually, use the Alpaca UI to place exits; the
   pipeline will reconcile them as CLOSED_EXTERNAL on next boot.
3. Recover the box (or a replacement): install Postgres 16, clone the repo,
   restore last night's dump (§3), start services (§6).
4. **Verified when:** first C4 reconciliation reports drift = adopted 0 /
   qty snaps only for anything you touched manually; `journal.health` rows
   all OK.

## 2. Broker outage (Alpaca API down, Spark fine)

- C4's periodic reconcile fails → `health.broker_api = DEGRADED`; dead-man
  keeps entries blocked if marketdata is also affected.
- Exits cannot be submitted; catastrophe stops (already resident broker-side)
  remain whatever the broker's own state is — an exchange-side outage is the
  one scenario stops can't cover. Nothing to do but watch.
- Do NOT restart services in a loop; C4 retries on its own.
- **Verified when:** `SELECT * FROM journal.health WHERE component='broker_api'`
  returns OK after the outage and the next reconcile summary shows no
  unexplained drift.

## 3. Postgres restore from backup

Nightly dumps: `~/pipeline-backups/trading-YYYYMMDD.dump` (14-day rotation,
written by `ops/backup.sh` from cron/systemd-timer).

```bash
sudo systemctl stop c4-exec a3-risk a1-triage a2-analyst c3-gate c1-ingestion
dropdb --if-exists trading && createdb trading -O trader
pg_restore -d trading --no-owner ~/pipeline-backups/trading-<DATE>.dump
$PSQL -c "SELECT count(*) FROM journal.decisions"   # sanity: non-zero
```
Start services (§6). C4's boot reconciliation will adopt/close anything that
happened at the broker after the dump was taken — read the reconcile summary
in the log before re-enabling entries.
**Verified when:** counts match expectations, reconciliation summary is
explainable, dashboard History tab renders.

## 4. Config rollback

Configs are git-versioned; every decision row carries `config_version`.
```bash
git log --oneline -- config/          # find the good version
git checkout <sha> -- config/
sudo systemctl restart a3-risk c4-exec c3-gate
$PSQL -c "SELECT config_version, registered_ts FROM journal.config_versions ORDER BY registered_ts DESC LIMIT 3"
```
**Verified when:** a new config_version row appears and new decisions
reference it.

## 5. Drawdown breaker / kill switch discipline

- Breaker trips at −2% daily on effective capital. It is **one-way**: code
  never resets it. Reset from the dashboard only after you have read the
  day's exits and understand the loss. Not before.
- Kill switch: blocks entries only. Exits and stop management continue.
- `block_entries` set by DEADMAN clears itself on heartbeat recovery; set by
  you, it stays until you clear it.

## 6. Cold start order (and the only order)

```bash
sudo systemctl start postgresql            # 1. store
sudo systemctl start qdrant                # 2. vectors (C2)
sudo systemctl start llama-a1 llama-a2     # 3. models (:8080, :8081)
sudo systemctl start c1-ingestion c2-dedup # 4. feed + dedup
sudo systemctl start c8-regime a1-triage a2-analyst c3-gate   # 5. brains (c8 first: A2 reads regime)
sudo systemctl start a3-risk               # 6. sizing
sudo systemctl start c4-exec               # 7. LAST: reconciles before consuming
```
**Verified when:** `journal.health` shows every component OK (including c2-dedup and c8-regime) and C4's log
prints the reconciliation summary before its first intent.

## 7. Backup verification (monthly, mandatory)

A backup that has never been restored is a hope, not a backup.
```bash
ops/backup.sh                                   # take one now
createdb trading_restore_test -O trader
pg_restore -d trading_restore_test --no-owner ~/pipeline-backups/<latest>
psql postgresql://trader:trader_dev@127.0.0.1:5432/trading_restore_test \
  -c "SELECT count(*) FROM journal.decisions" \
  -c "SELECT count(*) FROM news.news_items"
dropdb trading_restore_test
```
**Verified when:** counts match the live DB at dump time.

