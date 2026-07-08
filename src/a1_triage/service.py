"""A1 Triage + Router service (Phase 2, observe-only).

Consumer loop per message on signal.triage (DedupedSignal, spec §5):
  1. run_triage() against the configured backend (grammar-constrained)
  2. compute routing facts (code)
  3. apply the four routing rules (pure function)
  4. ONE TRANSACTION: journal.decisions row + all routing enqueues
     — a decision can't exist without its routing, and vice versa
  5. material=true -> promote to the retrieval collection (outside the tx;
     idempotent upsert, safe under at-least-once redelivery)
  6. ack

TriageRejected -> REJECT decision row with the raw output in payload; ack
(the message is handled — the failure is journaled, not retried forever).
Infrastructure errors -> queue.fail() -> backoff -> DLQ (spec §1).

TriagedSignal enqueued downstream = spec §6 shape: item_ref + triage + routing.
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal

from common.config import config_path, load_yaml
from common.contracts import envelope
from common.db import close_pool, get_pool
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from common.queue import ack, claim, enqueue, fail, wait_for_message
from c1_ingestion.heartbeat import set_health
from c2_dedup.embedder import embed_text_for, get_embedder
from c2_dedup.vectorstore import VectorStore
from router.facts import compute_facts
from router.rules import route

from .backends import get_backend
from .triage import TriageRejected, run_triage

log = get_logger("a1.service")

IN_QUEUE = "signal.triage"
CONSUMER = f"a1-{os.getpid()}"
CONTRACT_TRIAGED = "signal.triaged/1"


class A1Service:
    def __init__(self, cfg: dict, backend=None, store: VectorStore | None = None):
        self.cfg = cfg
        self.backend = backend or get_backend(cfg["model"])
        self.retries = int(cfg["model"].get("retries_on_invalid", 1))
        self.router_cfg = cfg["router"]
        self.store = store or VectorStore()
        self.embedder = get_embedder()

    async def handle(self, msg) -> None:
        body = msg.payload.get("body") or {}
        item = body.get("item") or {}
        cluster = body.get("cluster") or {}
        item_id = item.get("item_id")
        revision = int(item.get("revision") or 1)
        if not item_id or not item.get("headline"):
            raise ValueError(f"malformed DedupedSignal ({msg.dedup_key})")
        signal_id = item_id                       # news-origin signals: signal_id = item_id

        try:
            result = await run_triage(self.backend, item, cluster, self.retries)
        except TriageRejected as rej:
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                stage="TRIAGE", agent="A1", action="REJECT",
                payload={"raw_output": rej.raw, "error": rej.detail,
                         "attempts": rej.attempts},
                reason=f"model output invalid after {rej.attempts} attempts",
                model_id=rej.model_id, latency_ms=rej.latency_ms)
            log.warning("triage REJECT journaled", extra=kv(item_id=item_id))
            return

        triage = result.triage
        facts = await compute_facts(
            tickers=triage.tickers, source_tier=int(item.get("source_tier", 3)),
            urgency=triage.urgency, novelty=triage.novelty_score,
            independent_outlets=int(cluster.get("independent_outlets", 1)),
            router_cfg=self.router_cfg)
        decision = route(triage, facts,
                         overnight_base=int(self.router_cfg.get("overnight_base", 50)))

        triaged_body = {
            "item_ref": {"item_id": item_id, "revision": revision,
                         "cluster_id": cluster.get("cluster_id")},
            "triage": triage.model_dump(),
            "routing": facts.payload(),
        }

        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=signal_id, item_id=item_id, item_revision=revision,
                    ticker=triage.tickers[0] if triage.tickers else None,
                    stage="TRIAGE", agent="A1", action=decision.action,
                    payload={"triage": triage.model_dump(),
                             "routing": facts.payload(),
                             "routes": [r.queue for r in decision.routes]},
                    reason=triage.reason,
                    model_id=result.model_id, latency_ms=result.latency_ms,
                    conn=conn)
                msg_out = envelope(CONTRACT_TRIAGED, "A1", signal_id, item_id,
                                   revision, triaged_body)
                msg_out["envelope"]["trace"]["decision_id"] = decision_id
                for r in decision.routes:
                    await enqueue(r.queue, f"{item_id}:{revision}", msg_out,
                                  priority=r.priority, conn=conn)

        if triage.material:
            vector = self.embedder.embed(
                embed_text_for(item.get("headline", ""), item.get("summary")))
            self.store.promote_to_retrieval(
                item_id, revision, vector,
                payload={"headline": item.get("headline"),
                         "tickers": triage.tickers,
                         "published_ts": item.get("published_ts")})

        log.info("triaged", extra=kv(
            item_id=item_id, rev=revision, action=decision.action,
            material=triage.material, tickers=",".join(triage.tickers) or "-",
            routes=",".join(r.queue for r in decision.routes) or "-",
            latency_ms=result.latency_ms))


async def consume_loop(svc: A1Service, stop: asyncio.Event) -> None:
    await set_health("triage", "OK", f"consuming {IN_QUEUE}")
    while not stop.is_set():
        msg = await claim(IN_QUEUE, CONSUMER)
        if msg is None:
            try:
                await asyncio.wait_for(wait_for_message(IN_QUEUE, timeout_secs=5.0), 6.0)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await svc.handle(msg)
            await ack(msg.msg_id)
        except Exception as e:
            log.error("message failed", extra=kv(msg_id=msg.msg_id, error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def main() -> None:
    cfg = load_yaml(config_path("a1.yaml"))
    await register_config_version("a1 triage service startup")
    svc = A1Service(cfg)
    log.info("A1 up", extra=kv(backend=cfg["model"].get("backend"),
                               model=svc.backend.model_id, consumer=CONSUMER))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await consume_loop(svc, stop)
    await set_health("triage", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
