"""Analyst-slot courtesy protocol: A13 is the lowest-priority tenant.

The Analyst llama-server (:8081) is shared with A2 (new theses) and A12
(position guard — capital protection). Neither may wait on chat. Before every
model call A13 checks the READY depth of the pipeline queues that feed the
slot and yields until they drain or `max_wait_secs` elapses (a human is
waiting; past that we proceed and flag the contention in the reply).

Residual risk, accepted and documented: a chat generation already in flight
when an A2/A12 request arrives finishes first. max_tokens is kept small in
config so that window stays in the seconds range.
"""
from __future__ import annotations

import asyncio

from common.db import get_pool
from common.log import get_logger, kv

log = get_logger("a13.slot")


async def pipeline_ready_depth(queues: list[str],
                               ignore_older_than_secs: float = 900) -> int:
    """READY depth of the queues feeding the Analyst slot.

    v0.5.4: messages older than `ignore_older_than_secs` are NOT counted.
    A live consumer drains its queue in seconds; anything sitting ready for
    15+ minutes is orphaned — today, concretely, signal.guard fan-outs with
    no A12 to consume them until Phase 5. Yielding to a queue nothing reads
    would block chat forever (observed: depth=44, all stale guard messages).
    Fresh pipeline work still always goes first."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT count(*) FROM queue.messages
               WHERE queue_name = ANY(%s)
                 AND done_ts IS NULL AND claimed_ts IS NULL
                 AND available_ts <= now()
                 AND enqueued_ts > now() - make_interval(secs => %s)""",
            (queues, ignore_older_than_secs))
        return (await cur.fetchone())[0]


async def yield_to_pipeline(slot_cfg: dict) -> tuple[float, bool]:
    """Wait until the pipeline queues feeding the Analyst slot are empty.

    Returns (seconds_waited, contended) — contended=True means we gave up
    waiting and are proceeding while pipeline work is still queued (the
    answer should mention possible added latency for the pipeline)."""
    queues = list(slot_cfg.get("yield_to_queues") or ["signal.analyst", "signal.guard"])
    poll = float(slot_cfg.get("poll_secs", 2.0))
    max_wait = float(slot_cfg.get("max_wait_secs", 90))
    stale = float(slot_cfg.get("ignore_older_than_secs", 900))

    waited = 0.0
    while True:
        depth = await pipeline_ready_depth(queues, stale)
        if depth == 0:
            return waited, False
        if waited >= max_wait:
            log.warning("proceeding despite pipeline backlog",
                        extra=kv(depth=depth, waited_s=round(waited, 1)))
            return waited, True
        log.info("yielding analyst slot to pipeline",
                 extra=kv(depth=depth, waited_s=round(waited, 1)))
        await asyncio.sleep(poll)
        waited += poll
