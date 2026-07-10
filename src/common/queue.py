"""Thin wrappers over the queue schema (news-store-schema.sql):
queue.claim_next / queue.ack / queue.fail plus enqueue with
ON CONFLICT (queue_name, dedup_key) DO NOTHING — duplicate enqueue is a no-op
(rule 19: at-least-once + consumer dedup).

LISTEN/NOTIFY: enqueue() NOTIFYs channel "q_<queue_name with dots replaced>";
consumers LISTEN and also poll on a timeout (belt and braces — a NOTIFY sent
while the consumer is disconnected is lost by design, the poll catches it).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import psycopg

from .db import get_pool, jb


def _channel(queue_name: str) -> str:
    return "q_" + queue_name.replace(".", "_")


async def enqueue(queue_name: str, dedup_key: str, payload: dict,
                  priority: int = 100, conn: psycopg.AsyncConnection | None = None) -> bool:
    """Insert a message; returns False if the dedup key already existed.

    Pass `conn` to enqueue inside an existing transaction (C1 does this so the
    news_items insert and the enqueue commit atomically).
    """
    sql = """
        INSERT INTO queue.messages (queue_name, dedup_key, priority, payload)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (queue_name, dedup_key) DO NOTHING
        RETURNING msg_id
    """
    async def _run(c: psycopg.AsyncConnection) -> bool:
        cur = await c.execute(sql, (queue_name, dedup_key, priority, jb(payload)))
        row = await cur.fetchone()
        if row is not None:
            await c.execute(f"NOTIFY {_channel(queue_name)}")
            return True
        return False

    if conn is not None:
        return await _run(conn)
    pool = await get_pool()
    async with pool.connection() as c:
        return await _run(c)


@dataclass
class Message:
    msg_id: int
    queue_name: str
    dedup_key: str
    priority: int
    payload: dict
    attempts: int
    enqueued_ts: datetime


async def claim(queue_name: str, consumer: str) -> Optional[Message]:
    """Claim the next ready message via queue.claim_next (SKIP LOCKED)."""
    pool = await get_pool()
    async with pool.connection() as c:
        cur = await c.execute("SELECT * FROM queue.claim_next(%s, %s)", (queue_name, consumer))
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
        rec = dict(zip(cols, row))
        return Message(
            msg_id=rec["msg_id"], queue_name=rec["queue_name"],
            dedup_key=rec["dedup_key"], priority=rec["priority"],
            payload=rec["payload"], attempts=rec["attempts"],
            enqueued_ts=rec["enqueued_ts"],
        )


async def ack(msg_id: int) -> None:
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("SELECT queue.ack(%s)", (msg_id,))


async def fail(msg_id: int, error: str) -> None:
    """Retry with linear backoff; past max_attempts -> DLQ into news.quarantine."""
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("SELECT queue.fail(%s, %s)", (msg_id, error[:500]))


async def wait_for_message(queue_name: str, timeout_secs: float = 5.0) -> None:
    """Block until a NOTIFY on the queue's channel or timeout. Dedicated
    connection per call site (LISTEN state is per-connection)."""
    import asyncio

    from .db import dsn
    async with await psycopg.AsyncConnection.connect(dsn(), autocommit=True) as c:
        await c.execute(f"LISTEN {_channel(queue_name)}")
        gen = c.notifies()
        try:
            await asyncio.wait_for(gen.__anext__(), timeout=timeout_secs)
        except (asyncio.TimeoutError, StopAsyncIteration):
            pass

