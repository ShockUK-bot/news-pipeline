"""C2 dedup service. Consumes signal.dedup, runs the dedup/cluster decision,
and enqueues a DedupedSignal (spec §5) on signal.triage. Consumer-side dedup
is inherent: signal.triage's dedup_key is the same "{item_id}:{revision}", so
an at-least-once redelivery of a dedup message re-enqueues as a no-op.

Failed messages route through queue.fail() -> linear backoff -> DLQ into
news.quarantine after max_attempts (spec §1); the consumer never crashes on a
bad message.
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal

from common.contracts import (CONTRACT_TRIAGE, ClusterInfo, DedupedSignal, envelope)
from common.db import close_pool
from common.log import get_logger, kv
from common.queue import ack, claim, enqueue, fail, wait_for_message
from c1_ingestion.heartbeat import set_health

from .cluster import Deduper
from .embedder import get_embedder
from .vectorstore import VectorStore

log = get_logger("c2.service")

DEDUP_QUEUE = "signal.dedup"
TRIAGE_QUEUE = "signal.triage"
CONSUMER = f"c2-{os.getpid()}"
PRUNE_INTERVAL = 3600.0
WINDOW_HOURS = 48


async def handle_message(msg, deduper: Deduper) -> None:
    body = msg.payload.get("body") or {}
    trace = (msg.payload.get("envelope") or {}).get("trace") or {}
    item_id = body.get("item_id") or trace.get("item_id")
    revision = body.get("revision") or trace.get("revision") or 1
    if not item_id or not body.get("headline"):
        raise ValueError(f"malformed dedup message: missing item fields ({msg.dedup_key})")

    decision = await deduper.process(body)

    ds = DedupedSignal(item=body, cluster=ClusterInfo(
        cluster_id=decision.cluster_id,
        is_new_story=decision.is_new_story,
        independent_outlets=decision.independent_outlets,
        total_items=decision.total_items,
        similarity_to_canonical=round(decision.similarity_to_canonical, 4),
    ))
    out = envelope(CONTRACT_TRIAGE, "C2", item_id, item_id, revision,
                   ds.model_dump())
    await enqueue(TRIAGE_QUEUE, f"{item_id}:{revision}", out)
    log.info("forwarded to triage",
             extra=kv(item_id=item_id, rev=revision, cluster=decision.cluster_id,
                      new_story=decision.is_new_story,
                      outlets=decision.independent_outlets))


async def consume_loop(deduper: Deduper, stop: asyncio.Event) -> None:
    await set_health("dedup", "OK", "consuming signal.dedup")
    while not stop.is_set():
        msg = await claim(DEDUP_QUEUE, CONSUMER)
        if msg is None:
            # idle: block on NOTIFY with a poll fallback
            try:
                await asyncio.wait_for(wait_for_message(DEDUP_QUEUE, timeout_secs=5.0), 6.0)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await handle_message(msg, deduper)
            await ack(msg.msg_id)
        except Exception as e:
            log.error("message failed", extra=kv(msg_id=msg.msg_id, error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def prune_loop(store: VectorStore, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            store.prune_dedup(WINDOW_HOURS)
        except Exception as e:
            log.error("prune failed", extra=kv(error=repr(e)[:200]))
        try:
            await asyncio.wait_for(stop.wait(), timeout=PRUNE_INTERVAL)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    embedder = get_embedder()
    store = VectorStore()
    deduper = Deduper(store, embedder)
    log.info("C2 up", extra=kv(embedder=embedder.name, consumer=CONSUMER))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    await asyncio.gather(consume_loop(deduper, stop), prune_loop(store, stop))
    await set_health("dedup", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
