"""Nightly compact extract of the trading history to GitHub (v0.25.2).

Writes the tables that matter for a month review as gzipped CSV (decisions
without payloads) and force pushes them as ONE commit to the orphan branch
`journal-extract` of the existing private remote, so the repo's history does
not grow. Not a full restore (that is the pg_dump); it is the off box record
of trades, measurements and proposals if the Spark is lost. No new
credentials: uses the remote the repo already pushes to.

Run: PYTHONPATH=src .venv/bin/python ops/journal_extract.py [--no-push] [--out DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

TABLES = {
    "positions": "SELECT * FROM journal.positions ORDER BY position_id",
    "exits": "SELECT * FROM journal.exits ORDER BY exit_id",
    "orders": "SELECT order_id, intent_id, position_id, broker_order_id, order_role, state, qty, limit_price, stop_price, submitted_ts, closed_ts FROM journal.orders ORDER BY order_id",
    "trade_metrics": "SELECT * FROM journal.trade_metrics ORDER BY position_id",
    "metric_rollups": "SELECT * FROM journal.metric_rollups ORDER BY period_start, granularity, metric",
    "proposals": "SELECT * FROM journal.proposals ORDER BY proposal_id",
    "guard_ledger": "SELECT * FROM journal.guard_ledger ORDER BY guard_id",
    "scanner_candidates": "SELECT candidate_id, ts, scan_date, ticker, status, reject_reason, metrics, item_id FROM journal.scanner_candidates WHERE status IN ('EMITTED','CAPPED') ORDER BY candidate_id",
    "scanner_counterfactuals": "SELECT * FROM journal.scanner_counterfactuals ORDER BY position_id, variant",
    "scanner_funnel_cf": "SELECT * FROM journal.scanner_funnel_cf ORDER BY candidate_id",
    "gate_counterfactuals": "SELECT * FROM journal.gate_counterfactuals ORDER BY cf_id",
    "counterfactuals": "SELECT cf_id, kind, exit_id, decision_id, ticker, anchor_ts, anchor_price, horizon_desc, outcome_r, computed_ts FROM journal.counterfactuals ORDER BY cf_id",
    "theses": "SELECT * FROM journal.theses ORDER BY thesis_id",
    "sectors": "SELECT * FROM journal.sectors ORDER BY ticker",
    "control": "SELECT * FROM journal.control ORDER BY key",
    "audit": "SELECT * FROM journal.audit ORDER BY audit_id",
    "config_versions": "SELECT * FROM journal.config_versions ORDER BY applied_ts",
    "decisions": "SELECT decision_id, ts, signal_id, item_id, ticker, stage, agent, action, veto_reason, left(reason, 300) AS reason, confidence, model_id, latency_ms, config_version FROM journal.decisions ORDER BY decision_id",
    "outbox": "SELECT message_id, created_ts, kind, subject, status, sent_ts FROM journal.outbox ORDER BY message_id",
    "health": "SELECT * FROM journal.health ORDER BY component",
}


async def dump(out: str) -> dict:
    from common.db import get_pool, close_pool
    pool = await get_pool()
    stats = {}
    async with pool.connection() as conn:
        for name, sql in TABLES.items():
            path = os.path.join(out, f"{name}.csv.gz")
            with gzip.open(path, "wb") as f:
                async with conn.cursor().copy(f"COPY ({sql}) TO STDOUT WITH CSV HEADER") as copy:
                    async for chunk in copy:
                        f.write(bytes(chunk))
            stats[name] = os.path.getsize(path)
    await close_pool()
    return stats


def push(out: str, repo: str, branch: str = "journal-extract") -> str:
    """Build a commit from `out` inside the MAIN repo's object store (a temporary
    index, no working tree changes) and force push it to the orphan branch, so
    the push uses exactly the credentials the repo already pushes with."""
    with open(os.path.join(out, "README.md"), "w") as f:
        f.write(f"# journal extract\n\nGenerated {datetime.now(timezone.utc).isoformat()} by ops/journal_extract.py "
                f"on the Spark. One commit, force pushed nightly; not a full restore (see the pg_dump).\n")
    env = {**os.environ, "GIT_INDEX_FILE": os.path.join(out, ".extract-index"),
           "GIT_AUTHOR_NAME": "Pipeline Build", "GIT_AUTHOR_EMAIL": "pipeline@local",
           "GIT_COMMITTER_NAME": "Pipeline Build", "GIT_COMMITTER_EMAIL": "pipeline@local"}
    subprocess.check_call(["git", "-C", repo, "--work-tree", out, "add", "-A", "--", "."], env=env)
    tree = subprocess.check_output(["git", "-C", repo, "write-tree"], env=env, text=True).strip()
    commit = subprocess.check_output(["git", "-C", repo, "commit-tree", tree, "-m",
                                      f"journal extract {datetime.now(timezone.utc).date()}"], env=env, text=True).strip()
    subprocess.check_call(["git", "-C", repo, "push", "-q", "--force", "origin", f"{commit}:refs/heads/{branch}"])
    return subprocess.check_output(["git", "-C", repo, "remote", "get-url", "origin"], text=True).strip()


async def set_health(status: str, detail: str) -> None:
    try:
        from c1_ingestion.heartbeat import set_health as sh
        await sh("journal_extract", status, detail)
    except Exception:                                    # noqa: BLE001
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    out = a.out or tempfile.mkdtemp(prefix="journal-extract-files-")
    os.makedirs(out, exist_ok=True)
    stats = asyncio.run(dump(out))
    total = sum(stats.values())
    print(f"extract: {len(stats)} tables, {total/1e6:.1f} MB gz in {out}")
    if a.no_push:
        asyncio.run(set_health("OK", f"extract only (no push): {len(stats)} tables, {total/1e6:.1f} MB"))
        return
    try:
        remote = push(out, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        asyncio.run(set_health("OK", f"{len(stats)} tables, {total/1e6:.1f} MB pushed to journal-extract"))
        print(f"pushed to {remote} branch journal-extract")
    except subprocess.CalledProcessError as e:
        asyncio.run(set_health("DEGRADED", f"push failed: {e}"))
        print(f"push failed: {e}")
    finally:
        if not a.out:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    main()
