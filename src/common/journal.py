"""journal.decisions writes + config_version registration.

Conventions enforced here (journal-schema-spec §2):
  * config_version = git SHA of the repo at service start, registered in
    journal.config_versions before the first decision. Outside a git checkout
    (dev/test), a deterministic content hash of config/ is used, prefixed
    "dev-" so real SHAs and dev hashes can't be confused.
  * decisions.payload carries the FULL structured output; promoted columns
    only where filtered/joined on.
  * Writers pass an open connection when the decision must commit atomically
    with something else (A1: decision row + routing fan-out in one tx).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from typing import Optional

import psycopg

from .db import get_pool, jb
from .log import get_logger, kv

log = get_logger("journal")

_config_version: str | None = None


def compute_config_version() -> str:
    """git SHA of HEAD if we're in a checkout; else content hash of config/."""
    repo_root = os.path.join(os.path.dirname(__file__), "..", "..")
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root,
                             capture_output=True, text=True, timeout=5)
        if sha.returncode == 0:
            return sha.stdout.strip()[:12]
    except (OSError, subprocess.TimeoutExpired):
        pass
    h = hashlib.sha256()
    cfg_dir = os.path.join(repo_root, "config")
    for name in sorted(os.listdir(cfg_dir)):
        p = os.path.join(cfg_dir, name)
        if os.path.isfile(p):
            h.update(name.encode())
            h.update(open(p, "rb").read())
    return "dev-" + h.hexdigest()[:10]


async def register_config_version(summary: str = "") -> str:
    """Idempotent — safe to call at every service startup. The version string
    is computed once per process, but the INSERT always runs (ON CONFLICT
    no-op), so the row exists even if the table was reset since the last call."""
    global _config_version
    if _config_version is None:
        _config_version = compute_config_version()
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO journal.config_versions (config_version, summary)
               VALUES (%s, %s) ON CONFLICT (config_version) DO NOTHING""",
            (_config_version, summary[:200]))
    log.info("config version active", extra=kv(config_version=_config_version))
    return _config_version


def active_config_version() -> str:
    if _config_version is None:
        raise RuntimeError("register_config_version() not called at startup")
    return _config_version


async def write_decision(*, signal_id: str, stage: str, agent: str, action: str,
                         item_id: Optional[str] = None,
                         item_revision: Optional[int] = None,
                         ticker: Optional[str] = None,
                         veto_reason: Optional[str] = None,
                         payload: Optional[dict] = None,
                         reason: Optional[str] = None,
                         confidence: Optional[float] = None,
                         model_id: Optional[str] = None,
                         latency_ms: Optional[int] = None,
                         regime_id: Optional[int] = None,
                         derived_from: Optional[int] = None,
                         conn: psycopg.AsyncConnection | None = None) -> int:
    """Insert one decision row; returns decision_id. Pass conn to join an
    existing transaction (decision + routing fan-out must commit together)."""
    sql = """INSERT INTO journal.decisions
             (signal_id, item_id, item_revision, derived_from, ticker, stage,
              agent, action, veto_reason, payload, reason, confidence,
              model_id, latency_ms, config_version, regime_id)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
             RETURNING decision_id"""
    params = (signal_id, item_id, item_revision, derived_from, ticker, stage,
              agent, action, veto_reason, jb(payload or {}), reason, confidence,
              model_id, latency_ms, active_config_version(), regime_id)

    if conn is not None:
        cur = await conn.execute(sql, params)
        return (await cur.fetchone())[0]
    pool = await get_pool()
    async with pool.connection() as c:
        cur = await c.execute(sql, params)
        return (await cur.fetchone())[0]

