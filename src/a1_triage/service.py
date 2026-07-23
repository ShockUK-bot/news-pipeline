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
from router.facts import compute_facts, open_position_ids
from router.rules import route

from .backends import get_backend
from .suppression import DEFAULTS as SUPP_DEFAULTS
from .suppression import corroboration_bypass, find_prior_verdict
from .triage import TriageRejected, run_triage

log = get_logger("a1.service")

IN_QUEUE = "signal.triage"
SYNTHETIC_QUEUE = "signal.synthetic"
SCANNER_QUEUE = "signal.scanner"
CONSUMER = f"a1-{os.getpid()}"
CONTRACT_TRIAGED = "signal.triaged/1"


async def _fetch_item_and_cluster(item_id: str, revision: int) -> tuple[dict, dict] | None:
    """Parent item + its cluster corroboration, for synthetic re-entry."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT item_id, revision, is_correction, source, source_tier,
                      headline, summary, symbols, channels, published_ts, received_ts
               FROM news.news_items WHERE item_id = %s AND revision = %s""",
            (item_id, revision))
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
        item = dict(zip(cols, row))
        item["published_ts"] = item["published_ts"].isoformat()
        item["received_ts"] = item["received_ts"].isoformat()
        cur = await conn.execute(
            """SELECT c.cluster_id, c.independent_outlets, c.total_items
               FROM news.cluster_members cm
               JOIN news.cluster_corroboration c ON c.cluster_id = cm.cluster_id
               WHERE cm.item_id = %s LIMIT 1""", (item_id,))
        crow = await cur.fetchone()
        cluster = ({"cluster_id": crow[0], "is_new_story": False,
                    "independent_outlets": crow[1], "total_items": crow[2],
                    "similarity_to_canonical": 1.0} if crow else
                   {"cluster_id": None, "is_new_story": False,
                    "independent_outlets": 1, "total_items": 1,
                    "similarity_to_canonical": 1.0})
        return item, cluster


class A1Service:
    def __init__(self, cfg: dict, backend=None, store: VectorStore | None = None):
        self.cfg = cfg
        self.backend = backend or get_backend(cfg["model"])
        self.retries = int(cfg["model"].get("retries_on_invalid", 1))
        self.router_cfg = cfg["router"]
        self.min_confidence = float(cfg["router"].get("min_confidence", 0.0))
        self.supp_cfg = {**SUPP_DEFAULTS, **(cfg.get("suppression") or {})}
        self.store = store or VectorStore()
        self.embedder = get_embedder()

    async def _suppress_repeat(self, item: dict, cluster: dict,
                               signal_id: str, revision: int) -> bool:
        """v0.4.7 story-level repeat suppression (see suppression.py).
        Returns True if the item was journaled as SUPPRESS (caller stops)."""
        if not self.supp_cfg.get("enabled", True):
            return False
        cluster_id = cluster.get("cluster_id")
        if not cluster_id or cluster.get("is_new_story"):
            return False
        if item.get("is_correction"):
            return False                       # corrections always re-triage
        prior = await find_prior_verdict(
            cluster_id, float(self.supp_cfg["window_hours"]))
        if prior is None:
            return False
        outlets_now = int(cluster.get("independent_outlets", 1))
        if corroboration_bypass(
                outlets_now, prior.independent_outlets,
                int(self.supp_cfg["corroboration_reescalate_threshold"])):
            return False                       # newly corroborated: re-triage
        # A12 mandate: anything touching a held name gets the full path.
        tickers = sorted({*(item.get("symbols") or []), *prior.tickers})
        if tickers and await open_position_ids(tickers):
            return False
        await write_decision(
            signal_id=signal_id, item_id=item["item_id"], item_revision=revision,
            ticker=prior.tickers[0] if prior.tickers else None,
            stage="TRIAGE", agent="A1", action="SUPPRESS",
            payload={"suppressed_by": prior.decision_id,
                     "prior_action": prior.action,
                     "cluster": {"cluster_id": cluster_id,
                                 "is_new_story": False,
                                 "independent_outlets": outlets_now}},
            reason=(f"story cluster {cluster_id} already triaged "
                    f"(decision {prior.decision_id}, {prior.action}) within "
                    f"{self.supp_cfg['window_hours']}h"),
            model_id=None, latency_ms=None)
        log.info("repeat suppressed", extra=kv(
            item_id=item["item_id"], rev=revision, cluster=cluster_id,
            prior_decision=prior.decision_id, prior_action=prior.action))
        return True

    async def handle(self, msg) -> None:
        body = msg.payload.get("body") or {}
        item = body.get("item") or {}
        cluster = body.get("cluster") or {}
        item_id = item.get("item_id")
        revision = int(item.get("revision") or 1)
        if not item_id or not item.get("headline"):
            raise ValueError(f"malformed DedupedSignal ({msg.dedup_key})")
        signal_id = item_id                       # news-origin signals: signal_id = item_id

        # v0.4.7 — story-level repeat suppression BEFORE the model call:
        # a repeat costs one SQL lookup, zero tokens, zero A2 escalations.
        if await self._suppress_repeat(item, cluster, signal_id, revision):
            return

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
                         overnight_base=int(self.router_cfg.get("overnight_base", 50)),
                         min_confidence=self.min_confidence)

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
                             "routes": [r.queue for r in decision.routes],
                             # cluster state at decision time — read back by
                             # the suppression corroboration bypass (v0.4.7)
                             "cluster": {
                                 "cluster_id": cluster.get("cluster_id"),
                                 "is_new_story": cluster.get("is_new_story"),
                                 "independent_outlets":
                                     int(cluster.get("independent_outlets", 1))}},
                    reason=triage.reason, confidence=triage.confidence,
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


    async def handle_synthetic(self, msg) -> None:
        """Sympathy-lane re-entry (spec §10): triage the parent item FOR the
        sympathy ticker — same gates, no shortcuts. signal_id = synthetic_id;
        derived_from carries lineage; trace.ticker overrides A2's primary."""
        body = msg.payload.get("body") or {}
        syn_id = body.get("synthetic_id")
        parent = body.get("derived_from_item") or {}
        if not syn_id or not parent.get("item_id") or not body.get("ticker"):
            raise ValueError(f"malformed SyntheticSignal ({msg.dedup_key})")

        fetched = await _fetch_item_and_cluster(parent["item_id"],
                                                int(parent.get("revision") or 1))
        if fetched is None:
            raise ValueError(f"parent item not found: {parent}")
        item, cluster = fetched
        item = {**item, "sympathy": {"ticker": body["ticker"],
                                     "relation": body.get("relation"),
                                     "rationale": body.get("rationale")}}

        try:
            result = await run_triage(self.backend, item, cluster, self.retries)
        except TriageRejected as rej:
            await write_decision(
                signal_id=syn_id, item_id=parent["item_id"],
                item_revision=parent.get("revision"), ticker=body["ticker"],
                stage="TRIAGE", agent="A1", action="REJECT",
                payload={"raw_output": rej.raw, "error": rej.detail,
                         "synthetic": True},
                reason="synthetic triage output invalid",
                model_id=rej.model_id, latency_ms=rej.latency_ms,
                derived_from=body.get("derived_from_decision"))
            return

        triage = result.triage
        facts = await compute_facts(
            tickers=[body["ticker"]], source_tier=int(item.get("source_tier", 3)),
            urgency=triage.urgency, novelty=triage.novelty_score,
            independent_outlets=int(cluster.get("independent_outlets", 1)),
            router_cfg=self.router_cfg)
        decision = route(triage, facts,
                         overnight_base=int(self.router_cfg.get("overnight_base", 50)),
                         min_confidence=self.min_confidence)

        triaged_body = {
            "item_ref": {"item_id": parent["item_id"],
                         "revision": parent.get("revision"),
                         "cluster_id": cluster.get("cluster_id")},
            "triage": triage.model_dump(),
            "routing": facts.payload(),
        }
        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=syn_id, item_id=parent["item_id"],
                    item_revision=parent.get("revision"), ticker=body["ticker"],
                    stage="TRIAGE", agent="A1", action=decision.action,
                    payload={"triage": triage.model_dump(),
                             "routing": facts.payload(), "synthetic": True,
                             "relation": body.get("relation")},
                    reason=triage.reason, confidence=triage.confidence,
                    model_id=result.model_id,
                    latency_ms=result.latency_ms,
                    derived_from=body.get("derived_from_decision"), conn=conn)
                out = envelope(CONTRACT_TRIAGED, "A1", syn_id,
                               parent["item_id"],
                               int(parent.get("revision") or 1), triaged_body)
                out["envelope"]["trace"]["decision_id"] = decision_id
                out["envelope"]["trace"]["ticker"] = body["ticker"]
                out["envelope"]["trace"]["derived_from_decision"] = \
                    body.get("derived_from_decision")
                for r in decision.routes:
                    await enqueue(r.queue, syn_id, out, priority=r.priority,
                                  conn=conn)
        log.info("synthetic triaged", extra=kv(
            synthetic_id=syn_id, ticker=body["ticker"],
            action=decision.action,
            routes=",".join(r.queue for r in decision.routes) or "-"))


    async def handle_scanner(self, msg) -> None:
        """Scanner-lane entry (v0.12.1). C10 already did the deterministic
        triage — the metrics ARE the escalation case, and there is no news
        text for the 8B to judge — so this handler is thin CODE: sanity
        checks, position-touching guard, one TRIAGE decision row, and a
        TriagedSignal to A2 with origin=scanner preserved in the trace.
        No model call, no shortcuts downstream: same A2 -> C3 -> A3 -> C4
        path as every other signal."""
        body = msg.payload.get("body") or {}
        scanner = body.get("scanner") or {}
        item_ref = body.get("item_ref") or {}
        ticker = body.get("ticker")
        item_id = item_ref.get("item_id")
        if not ticker or not item_id or not scanner:
            raise ValueError(f"malformed ScannerSignal ({msg.dedup_key})")
        signal_id = item_id                    # scanner:<ticker>:<date>

        # Belt + braces: C10 filters held names, but the queue is async —
        # a position opened between scan and claim must not be added to.
        if await open_position_ids([ticker]):
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=1,
                ticker=ticker, stage="TRIAGE", agent="A1", action="DISCARD",
                payload={"scanner": scanner, "origin": "scanner"},
                reason="scanner signal on already-held name",
                model_id=None, latency_ms=None)
            log.info("scanner DISCARD (held)", extra=kv(ticker=ticker))
            return

        triage = {"material": True, "tickers": [ticker],
                  "direction_hint": "up", "urgency": "high",
                  "novelty_score": 1.0,
                  "confidence": None,
                  "reason": ("deterministic scanner detection: "
                             f"{scanner.get('move_pct')} move on "
                             f"{scanner.get('rel_volume')}x relative volume, "
                             f"news_match={scanner.get('news_match')}")}
        triaged_body = {"item_ref": item_ref, "triage": triage,
                        "routing": {"origin": "scanner"},
                        "scanner": scanner, "origin": "scanner"}
        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=signal_id, item_id=item_id, item_revision=1,
                    ticker=ticker, stage="TRIAGE", agent="A1",
                    action="ESCALATE",
                    payload={"scanner": scanner, "origin": "scanner",
                             "routes": ["signal.analyst"]},
                    reason=triage["reason"], model_id=None, latency_ms=None,
                    conn=conn)
                out = envelope(CONTRACT_TRIAGED, "A1", signal_id, item_id, 1,
                               triaged_body)
                out["envelope"]["trace"]["decision_id"] = decision_id
                out["envelope"]["trace"]["ticker"] = ticker
                out["envelope"]["trace"]["origin"] = "scanner"
                await enqueue("signal.analyst", signal_id, out, conn=conn)
        log.info("scanner triaged", extra=kv(ticker=ticker,
                                             signal_id=signal_id))


async def consume_loop(svc: A1Service, stop: asyncio.Event) -> None:
    import time
    hb_detail = f"consuming {IN_QUEUE} + {SYNTHETIC_QUEUE} + {SCANNER_QUEUE}"
    await set_health("triage", "OK", hb_detail)
    last_hb = time.monotonic()
    while not stop.is_set():
        # Periodic heartbeat (v0.11.7) — A1 used to write health only at
        # startup, so the dead-man could not tell a live A1 from a dead one.
        if time.monotonic() - last_hb >= 60.0:
            await set_health("triage", "OK", hb_detail)
            last_hb = time.monotonic()
        msg = await claim(IN_QUEUE, CONSUMER)
        handler = svc.handle
        if msg is None:
            msg = await claim(SYNTHETIC_QUEUE, CONSUMER)
            handler = svc.handle_synthetic
        if msg is None:
            msg = await claim(SCANNER_QUEUE, CONSUMER)
            handler = svc.handle_scanner
        if msg is None:
            try:
                await asyncio.wait_for(wait_for_message(IN_QUEUE, timeout_secs=5.0), 6.0)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await handler(msg)
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

