"""A2 Analyst service (Phase 3, observe-only).

Per message on signal.analyst (TriagedSignal, spec §6):
  1. fetch the item's latest revision from the news store (TriagedSignal
     carries item_ref only — the store is the source of truth)
  2. build the context pack (code): price action since news, related
     headlines from retrieval, regime features
  3. run the analyst model (grammar-constrained ThesisOutput; machine
     invalidations DSL-validated at parse time), one retry, else REJECT
  4. ONE TRANSACTION: ANALYST decision row + signal.gate enqueue (§7 shape)
     + one signal.synthetic enqueue per related opportunity (§10 shape)
  5. ack

Model slot: Analyst (Qwen3-32B Q5_K_M, llama-server :8081). Same retry/REJECT
discipline as A1. Down-direction theses still journal + gate (C3 handles the
long-only veto) — a bearish read is information, not an error.
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
from common.marketdata import get_marketdata
from c1_ingestion.heartbeat import set_health
from c2_dedup.embedder import get_embedder
from c2_dedup.vectorstore import VectorStore
from a1_triage.backends import get_backend
from a1_triage.triage import TriageRejected

from .context import build_context
from .prompt import build_messages
from .schema import ThesisValidationError, thesis_json_schema, validate_thesis

log = get_logger("a2.service")

IN_QUEUE = "signal.analyst"
GATE_QUEUE = "signal.gate"
SYNTHETIC_QUEUE = "signal.synthetic"
CONSUMER = f"a2-{os.getpid()}"
CONTRACT_THESIS = "signal.gate/1"
CONTRACT_SYNTHETIC = "signal.synthetic/1"


async def fetch_item(item_id: str, revision: int) -> dict | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT item_id, revision, is_correction, source, source_tier,
                      source_url, headline, summary, symbols, channels,
                      published_ts, received_ts
               FROM news.news_items WHERE item_id = %s AND revision = %s""",
            (item_id, revision))
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
        item = dict(zip(cols, row))
        item["published_ts"] = item["published_ts"].isoformat()
        item["received_ts"] = item["received_ts"].isoformat()
        return item


class A2Service:
    def __init__(self, cfg: dict, backend=None, md=None, store=None):
        self.cfg = cfg
        self.backend = backend or get_backend(cfg["model"])
        self.retries = int(cfg["model"].get("retries_on_invalid", 1))
        self.md = md or get_marketdata()
        self.store = store or VectorStore()
        self.embedder = get_embedder()

    async def handle(self, msg) -> None:
        body = msg.payload.get("body") or {}
        item_ref = body.get("item_ref") or {}
        triage = body.get("triage") or {}
        item_id = item_ref.get("item_id")
        revision = int(item_ref.get("revision") or 1)
        signal_id = (msg.payload.get("envelope", {}).get("trace", {})
                     .get("signal_id") or item_id)
        derived_from = (msg.payload.get("envelope", {}).get("trace", {})
                        .get("derived_from_decision"))
        if not item_id:
            raise ValueError(f"malformed TriagedSignal ({msg.dedup_key})")

        item = await fetch_item(item_id, revision)
        if item is None:
            raise ValueError(f"item not found in news store: {item_id} rev {revision}")

        # primary ticker: triage's first (synthetic signals override via trace)
        ticker = (msg.payload.get("envelope", {}).get("trace", {}).get("ticker")
                  or (triage.get("tickers") or [None])[0])
        if not ticker:
            raise ValueError(f"no ticker on analyst-lane signal {signal_id}")

        context, regime_id = await build_context(self.md, self.store,
                                                 self.embedder, item, ticker)

        schema = thesis_json_schema()
        error: ThesisValidationError | None = None
        total_latency = 0
        for attempt in range(1 + self.retries):
            messages = build_messages(item, triage, context,
                                      retry_error=error.detail if error else None)
            reply = await self.backend.complete(messages, schema)
            total_latency += reply.latency_ms
            try:
                thesis = validate_thesis(reply.text)
                break
            except ThesisValidationError as e:
                error = e
                log.warning("invalid thesis output",
                            extra=kv(attempt=attempt + 1, detail=e.detail[:150]))
        else:
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                ticker=ticker, stage="ANALYST", agent="A2", action="REJECT",
                payload={"raw_output": error.raw, "error": error.detail,
                         "attempts": 1 + self.retries},
                reason=f"model output invalid after {1 + self.retries} attempts",
                model_id=reply.model_id, latency_ms=total_latency,
                regime_id=regime_id, derived_from=derived_from)
            log.warning("thesis REJECT journaled", extra=kv(item_id=item_id))
            return

        gate_body = {"item_ref": item_ref,
                     "thesis": thesis.model_dump(),
                     "regime_id": regime_id}

        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=signal_id, item_id=item_id, item_revision=revision,
                    ticker=thesis.ticker, stage="ANALYST", agent="A2",
                    action="THESIS",
                    payload={"thesis": thesis.model_dump(), "context": context},
                    reason=thesis.reason, confidence=thesis.confidence,
                    model_id=self.backend.model_id, latency_ms=total_latency,
                    regime_id=regime_id, derived_from=derived_from, conn=conn)

                out = envelope(CONTRACT_THESIS, "A2", signal_id, item_id,
                               revision, gate_body)
                out["envelope"]["trace"]["decision_id"] = decision_id
                await enqueue(GATE_QUEUE, f"{signal_id}:{revision}", out, conn=conn)

                # Sympathy fan-out (spec §10): ONLY a primary thesis — one
                # derived directly from a real news item — may spawn synthetic
                # sympathy signals. A synthetic-derived thesis must NOT fan out
                # again, or a single story naming several tickers becomes a
                # self-sustaining A2->synthetic->A1->A2 loop, each name
                # re-triggering the others without end (2026-07-20 incident: a
                # 3-restaurant food-safety story re-analyzed ~90x/hour, all
                # LONG_ONLY-vetoed, saturating the analyst slot). `derived_from`
                # is None on primary analyst signals and set on synthetic-origin
                # ones (A1.handle_synthetic stamps the trace), so it is exactly
                # the depth guard: sympathy stays one hop from real news.
                if derived_from is None:
                    for opp in thesis.related_opportunities:
                        syn_id = f"syn-{decision_id}-{opp.ticker}"
                        syn = envelope(CONTRACT_SYNTHETIC, "A2", syn_id, item_id,
                                       revision, {
                                           "synthetic_id": syn_id,
                                           "derived_from_decision": decision_id,
                                           "derived_from_item": {"item_id": item_id,
                                                                 "revision": revision},
                                           "ticker": opp.ticker,
                                           "relation": opp.relation,
                                           "rationale": opp.rationale,
                                       })
                        await enqueue(SYNTHETIC_QUEUE, syn_id, syn, conn=conn)

        log.info("thesis", extra=kv(
            signal_id=signal_id, ticker=thesis.ticker, dir=thesis.direction,
            mag=thesis.magnitude_est, conf=thesis.confidence,
            synthetics=(len(thesis.related_opportunities)
                        if derived_from is None else 0),
            latency_ms=total_latency))


async def consume_loop(svc: A2Service, stop: asyncio.Event) -> None:
    import time
    hb_detail = f"consuming {IN_QUEUE}"
    await set_health("analyst", "OK", hb_detail)
    last_hb = time.monotonic()
    while not stop.is_set():
        if time.monotonic() - last_hb >= 60.0:
            await set_health("analyst", "OK", hb_detail)
            last_hb = time.monotonic()
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
    cfg = load_yaml(config_path("a2.yaml"))
    await register_config_version("a2 analyst service startup")
    svc = A2Service(cfg)
    log.info("A2 up", extra=kv(backend=cfg["model"].get("backend"),
                               model=svc.backend.model_id, consumer=CONSUMER))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await consume_loop(svc, stop)
    await set_health("analyst", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

