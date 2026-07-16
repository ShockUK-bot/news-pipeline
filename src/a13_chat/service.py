"""A13 Operator Chat service.

Consumer loop per message on chat.request (enqueued by the C6 dashboard):
  kind=ASK  — 1. yield the Analyst slot to pipeline work (slot.py)
              2. planner call (grammar-constrained) -> validated pack list;
                 invalid after retries -> deterministic fallback plan
              3. run the packs (code-side parameterized SQL only)
              4. yield again, then answer call -> validated AnswerOutput
              5. insert the ASSISTANT/ANSWER chat row (fact_sheet attached,
                 filing proposal carried verbatim), mark the ASK row DONE
  kind=FILE — operator confirmed a filing proposal on the dashboard (token
              checked there). filing.py's code gates dispose; either way the
              outcome lands as a FILE_RESULT chat row + audit trail.

A13 never touches positions, orders, stops, or control flags. Its decisions
namespace is stage='CHAT' and its only enqueue is signal.synthetic via
filing.py.
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal

from common.config import config_path, load_yaml
from common.db import close_pool, get_pool, jb
from common.journal import register_config_version
from common.log import get_logger, kv
from common.queue import ack, claim, fail, wait_for_message
from c1_ingestion.heartbeat import set_health
from a1_triage.backends import get_backend

from .filing import FilingRejected, file_for_evaluation
from .prompt import build_answer_messages, build_planner_messages
from .retrieval import run_queries, truncate_fact_sheet
from .schema import (AnswerOutput, ChatValidationError, FilingProposal,
                     PlannedQuery, PlannerOutput, answer_json_schema,
                     planner_json_schema, validate_answer, validate_plan)
from .slot import yield_to_pipeline

log = get_logger("a13.service")

IN_QUEUE = "chat.request"
CONSUMER = f"a13-{os.getpid()}"


def fallback_plan() -> PlannerOutput:
    """Deterministic plan when the planner call fails validation: a broad
    journal snapshot. Wrong-but-safe beats no answer for an operator tool."""
    return PlannerOutput(
        queries=[PlannedQuery(query="open_positions"),
                 PlannedQuery(query="closed_trades", days=7),
                 PlannedQuery(query="vetoes", days=7),
                 PlannedQuery(query="control_state")],
        reason="fallback plan: planner output invalid")


class A13Service:
    def __init__(self, cfg: dict, backend=None, md=None):
        self.cfg = cfg
        self.backend = backend or get_backend(cfg["model"])
        self.retries = int(cfg["model"].get("retries_on_invalid", 1))
        self.slot_cfg = cfg.get("slot") or {}
        self.max_context = int((cfg.get("answer") or {}).get("max_context_chars", 24000))
        self.md = md

    # -- chat-row helpers ----------------------------------------------------

    async def _fetch_message(self, message_id: int) -> dict | None:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT message_id, session_id, role, kind, content, proposal
                   FROM journal.chat_messages WHERE message_id = %s""",
                (message_id,))
            row = await cur.fetchone()
            if row is None:
                return None
            cols = [d.name for d in cur.description]
            return dict(zip(cols, row))

    async def _reply(self, req: dict, kind: str, content: str, *,
                     fact_sheet: dict | None = None, proposal: dict | None = None,
                     decision_id: int | None = None, model_id: str | None = None,
                     latency_ms: int | None = None,
                     req_status: str = "DONE") -> int:
        """ONE TRANSACTION: assistant row + request-row status flip."""
        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """INSERT INTO journal.chat_messages
                       (session_id, role, kind, content, reply_to, fact_sheet,
                        proposal, decision_id, model_id, latency_ms, status)
                       VALUES (%s,'ASSISTANT',%s,%s,%s,%s,%s,%s,%s,%s,'DONE')
                       RETURNING message_id""",
                    (req["session_id"], kind, content, req["message_id"],
                     jb(fact_sheet) if fact_sheet is not None else None,
                     jb(proposal) if proposal is not None else None,
                     decision_id, model_id, latency_ms))
                reply_id = (await cur.fetchone())[0]
                await conn.execute(
                    "UPDATE journal.chat_messages SET status = %s WHERE message_id = %s",
                    (req_status, req["message_id"]))
        return reply_id

    # -- model calls ----------------------------------------------------------

    async def _plan(self, question: str) -> tuple[PlannerOutput, int]:
        schema = planner_json_schema()
        error: ChatValidationError | None = None
        total = 0
        for _ in range(1 + self.retries):
            messages = build_planner_messages(
                question, retry_error=error.detail if error else None)
            reply = await self.backend.complete(messages, schema)
            total += reply.latency_ms
            try:
                return validate_plan(reply.text), total
            except ChatValidationError as e:
                error = e
                log.warning("invalid planner output", extra=kv(detail=e.detail[:150]))
        return fallback_plan(), total

    async def _answer(self, question: str, fact_sheet: dict,
                      notes: list[str]) -> tuple[AnswerOutput, int]:
        schema = answer_json_schema()
        error: ChatValidationError | None = None
        total = 0
        for _ in range(1 + self.retries):
            messages = build_answer_messages(
                question, fact_sheet, notes,
                retry_error=error.detail if error else None)
            reply = await self.backend.complete(messages, schema)
            total += reply.latency_ms
            try:
                return validate_answer(reply.text), total
            except ChatValidationError as e:
                error = e
                log.warning("invalid answer output", extra=kv(detail=e.detail[:150]))
        raise error  # type: ignore[misc]

    # -- handlers --------------------------------------------------------------

    async def handle_ask(self, req: dict) -> None:
        question = req["content"]
        notes: list[str] = []

        waited, contended = await yield_to_pipeline(self.slot_cfg)
        if contended:
            notes.append("analyst slot contended: pipeline work was still queued "
                         "when this answer was generated")

        plan, plan_latency = await self._plan(question)
        fact_sheet = await run_queries(plan.queries, self.cfg, md=self.md)
        fact_sheet = truncate_fact_sheet(fact_sheet, self.max_context)
        if fact_sheet.get("_truncated"):
            notes.append(f"fact sheet truncated: {fact_sheet['_truncated']}")

        _, contended2 = await yield_to_pipeline(self.slot_cfg)
        if contended2 and not contended:
            notes.append("analyst slot contended during answer generation")

        try:
            answer, ans_latency = await self._answer(question, fact_sheet, notes)
        except ChatValidationError as e:
            await self._reply(req, "ERROR",
                              f"model output invalid after {1 + self.retries} "
                              f"attempts: {e.detail}",
                              fact_sheet=fact_sheet, req_status="ERROR")
            log.warning("answer REJECT journaled", extra=kv(message_id=req["message_id"]))
            return

        content = answer.answer
        if answer.recommendation:
            content += (f"\n\nRECOMMENDATION [{answer.recommendation.stance}] "
                        f"(advisory only): {answer.recommendation.rationale}")
        if answer.caveats:
            content += "\n\nCaveats: " + "; ".join(answer.caveats)

        proposal = answer.filing_proposal.model_dump() if answer.filing_proposal else None
        await self._reply(req, "ANSWER", content, fact_sheet=fact_sheet,
                          proposal=proposal, model_id=self.backend.model_id,
                          latency_ms=plan_latency + ans_latency)
        log.info("answered", extra=kv(
            message_id=req["message_id"], packs=len(plan.queries),
            proposal=bool(proposal), waited_s=round(waited, 1),
            latency_ms=plan_latency + ans_latency))

    async def handle_file(self, req: dict) -> None:
        raw = req.get("proposal") or {}
        operator = (raw.get("operator") or "dashboard")[:80]
        try:
            proposal = FilingProposal(
                ticker=raw.get("ticker", ""),
                anchor_item_id=raw.get("anchor_item_id", ""),
                rationale=raw.get("rationale", "operator filing"))
        except Exception as e:                            # noqa: BLE001
            await self._reply(req, "FILE_RESULT",
                              f"filing rejected (BAD_PROPOSAL): {repr(e)[:200]}",
                              req_status="ERROR")
            return

        try:
            decision_id, syn_id = await file_for_evaluation(
                proposal, operator, req["message_id"], self.cfg)
        except FilingRejected as rej:
            await self._reply(req, "FILE_RESULT",
                              f"filing rejected ({rej.code}): {rej.detail}",
                              proposal=proposal.model_dump())
            return

        await self._reply(
            req, "FILE_RESULT",
            f"{proposal.ticker} filed for evaluation (decision {decision_id}, "
            f"signal {syn_id}). It now runs the full pipeline — triage, analyst, "
            f"confirmation gate, risk — with no shortcuts; watch the decision "
            f"tape for the outcome.",
            proposal=proposal.model_dump(), decision_id=decision_id)

    async def handle(self, msg) -> None:
        body = msg.payload.get("body") or {}
        message_id = body.get("message_id")
        if not message_id:
            raise ValueError(f"malformed chat.request ({msg.dedup_key})")
        req = await self._fetch_message(int(message_id))
        if req is None:
            raise ValueError(f"chat message not found: {message_id}")
        # at-least-once idempotency: if a reply already exists, we're done
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM journal.chat_messages WHERE reply_to = %s LIMIT 1",
                (req["message_id"],))
            if await cur.fetchone():
                log.info("duplicate delivery ignored", extra=kv(message_id=message_id))
                return
        if req["kind"] == "FILE_REQUEST":
            await self.handle_file(req)
        else:
            await self.handle_ask(req)


async def consume_loop(svc: A13Service, stop: asyncio.Event) -> None:
    await set_health("chat", "OK", f"consuming {IN_QUEUE}")
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
        except Exception as e:                            # noqa: BLE001
            log.error("message failed", extra=kv(msg_id=msg.msg_id, error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def main() -> None:
    cfg = load_yaml(config_path("a13.yaml"))
    await register_config_version("a13 chat service startup")
    svc = A13Service(cfg)
    log.info("A13 up", extra=kv(backend=cfg["model"].get("backend"),
                                model=svc.backend.model_id, consumer=CONSUMER))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await consume_loop(svc, stop)
    await set_health("chat", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
