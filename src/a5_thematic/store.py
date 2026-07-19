"""Thesis-store access — the ONLY writer of journal.theses /
journal.thesis_evidence (A5's mandate; everyone else reads).

All mutations take an open connection so they commit atomically with their
journal decision rows (house rule: the journal is written with, never after,
the side effect). Redelivered evidence is a no-op via the UNIQUE constraint
on (thesis_id, item_id, item_revision).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from common.clock import utcnow
from common.db import get_pool, jb
from common.log import get_logger, kv

log = get_logger("a5.store")


def make_thesis_id(year: int, seq: int) -> str:
    return f"th-{year}-{seq:03d}"


def stale_cutoff(now: datetime, stale_weeks: float) -> datetime:
    return now - timedelta(weeks=stale_weeks)


async def load_active() -> list[dict]:
    """Compact view of every ACTIVE thesis (model input + validation set)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT thesis_id, title, driver, direction, horizon, confidence,
                      beneficiaries, invalidation, last_evidence_ts,
                      evidence_count, created_ts
               FROM journal.theses WHERE status='ACTIVE'
               ORDER BY confidence DESC, updated_ts DESC""")
        rows = await cur.fetchall()
    now = utcnow()
    out = []
    for r in rows:
        ref = r[8] or r[10]
        out.append({
            "thesis_id": r[0], "title": r[1], "driver": r[2],
            "direction": r[3], "horizon": r[4], "confidence": r[5],
            "beneficiaries": r[6], "invalidation": r[7],
            "evidence_count": r[9],
            "days_since_evidence": round((now - ref).total_seconds() / 86400, 1),
        })
    return out


async def next_thesis_id(conn, now: datetime) -> str:
    cur = await conn.execute("SELECT nextval('journal.thesis_seq')")
    seq = (await cur.fetchone())[0]
    return make_thesis_id(now.year, int(seq))


async def create_thesis(conn, thesis_id: str, spec, config_version: str,
                        decision_id: int) -> None:
    """spec: a validated NewThesis."""
    await conn.execute(
        """INSERT INTO journal.theses
             (thesis_id, title, driver, direction, horizon, confidence,
              beneficiaries, invalidation, created_decision_id,
              config_version)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (thesis_id, spec.title, spec.driver, spec.direction, spec.horizon,
         spec.confidence,
         jb([b.model_dump() for b in spec.beneficiaries]),
         jb(list(spec.invalidation)), decision_id, config_version))


async def add_evidence(conn, thesis_id: str, item_id: str, item_revision: int,
                       polarity: str, note: str, decision_id: int,
                       ts: datetime) -> bool:
    """Returns False when this (thesis, item, revision) was already logged."""
    cur = await conn.execute(
        """INSERT INTO journal.thesis_evidence
             (thesis_id, ts, item_id, item_revision, polarity, note,
              decision_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (thesis_id, item_id, item_revision) DO NOTHING
           RETURNING evidence_id""",
        (thesis_id, ts, item_id, item_revision, polarity, note, decision_id))
    if await cur.fetchone() is None:
        return False
    await conn.execute(
        """UPDATE journal.theses
           SET last_evidence_ts=%s, evidence_count=evidence_count+1,
               updated_ts=now()
           WHERE thesis_id=%s""", (ts, thesis_id))
    return True


async def set_status(conn, thesis_id: str, status: str,
                     decision_id: int) -> None:
    await conn.execute(
        """UPDATE journal.theses
           SET status=%s, status_decision_id=%s, updated_ts=now()
           WHERE thesis_id=%s AND status='ACTIVE'""",
        (status, decision_id, thesis_id))


async def set_confidence(conn, thesis_id: str, confidence: float) -> None:
    await conn.execute(
        """UPDATE journal.theses SET confidence=%s, updated_ts=now()
           WHERE thesis_id=%s AND status='ACTIVE'""",
        (confidence, thesis_id))


async def stale_active(cutoff: datetime) -> list[dict]:
    """ACTIVE theses whose evidence clock (last evidence, else creation) is
    older than the cutoff — the code-side expiry rule (baseline L3, long
    lane: 'no confirming evidence added in N weeks')."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT thesis_id, title, COALESCE(last_evidence_ts, created_ts)
               FROM journal.theses
               WHERE status='ACTIVE'
                 AND COALESCE(last_evidence_ts, created_ts) < %s""",
            (cutoff,))
        return [{"thesis_id": r[0], "title": r[1], "last_evidence_ts": r[2]}
                for r in await cur.fetchall()]
