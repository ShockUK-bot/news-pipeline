"""A8 Morning Briefing (Phase 9) — oneshot, fired by a8-briefing.timer at
07:35 ET on weekdays, after A4's 07:00 run. The last email in the baseline
inventory: ONE consolidated pre-open briefing replacing A4's bare sheet
email (v0.11.0 ships config/a4.yaml with report.email: false — A4 still
journals its SHEET decision; A8 embeds the sheet here).

Flow (A7 skeleton):
  1. code builds the consolidated fact sheet from the journal (facts.py):
     A4 sheet + thesis store + open positions with earnings clocks + A6
     recommendations + today's earnings landscape + queue/health status.
     Every section degrades independently — the briefing always ships.
  2. narrative slot: probe-first ONLY for the heavy model (autostart is
     OFF at 07:35 — pre-open is inside the memory rule's caution window
     and A4 already stopped the slot it started at 07:00), so in practice
     the resident/woken ANALYST slot narrates; a running heavy is used if
     the operator left one up.
  3. grammar-constrained narrative; invalid after retry -> briefing ships
     WITHOUT narrative (the email is never blocked by an LLM).
  4. ONE TRANSACTION: SYSTEM/A8 BRIEFING decision row + journal.outbox
     insert (kind MORNING_BRIEFING) — C5 mails it minutes later.

Idempotent per session date via the BRIEFING decision; non-session days
journal SKIPPED_NO_SESSION (report.send_on_nonsession overrides for manual
weekend runs, as everywhere).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from common.config import config_path, load_yaml
from common.clock import utcnow
from common.db import get_pool, jb
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from a4_premarket.service import session_today
from a7_report.service import SlotManager

from .facts import build_facts
from .narrative import (NarrativeValidationError, build_messages,
                        narrative_json_schema, validate_narrative)
from .render import render, subject_line

log = get_logger("a8.briefing")

ET = ZoneInfo("America/New_York")
KIND = "MORNING_BRIEFING"


async def already_briefed(session_date: str) -> bool:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT 1 FROM journal.decisions
               WHERE stage='SYSTEM' AND agent='A8' AND action='BRIEFING'
                 AND payload->>'session_date' = %s LIMIT 1""",
            (session_date,))
        return (await cur.fetchone()) is not None


async def generate_narrative(backend, facts: dict, retries: int):
    """Returns (BriefingNarrative | None, model_id | None, latency_ms)."""
    if backend is None:
        return None, None, 0
    error: NarrativeValidationError | None = None
    total = 0
    for _ in range(1 + retries):
        messages = build_messages(facts,
                                  retry_error=error.detail if error else None)
        try:
            reply = await backend.complete(messages, narrative_json_schema())
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            log.warning("narrative call failed — shipping without narrative",
                        extra=kv(error=repr(e)[:200]))
            return None, None, total
        total += reply.latency_ms
        try:
            return validate_narrative(reply.text), reply.model_id, total
        except NarrativeValidationError as e:
            error = e
            log.warning("invalid narrative output",
                        extra=kv(detail=e.detail[:150]))
    return None, backend.model_id, total


async def run_briefing(cfg: dict, backend_override=None,
                       now: datetime | None = None) -> int | None:
    """Returns the briefing's outbox message_id, or None when skipped."""
    now = now or utcnow()
    session_date = now.astimezone(ET).strftime("%Y-%m-%d")

    if await already_briefed(session_date):
        log.info("briefing already exists — skipping",
                 extra=kv(date=session_date))
        return None
    if not session_today(now) and not (cfg.get("report") or {}).get(
            "send_on_nonsession", False):
        await write_decision(
            signal_id=f"briefing-{session_date}", stage="SYSTEM", agent="A8",
            action="SKIPPED_NO_SESSION",
            payload={"session_date": session_date},
            reason="no NYSE session today")
        return None

    facts = await build_facts(
        now, blackout_warn=int((cfg.get("briefing") or {})
                               .get("blackout_warn_sessions", 2)))

    slots = SlotManager(cfg)
    narrative = model_id = None
    latency = 0
    try:
        if backend_override is not None:
            backend, slot_name = backend_override, "stub"
        else:
            backend, slot_name = await slots.acquire()
        retries = int((cfg.get("narrative") or {})
                      .get("retries_on_invalid", 1))
        narrative, model_id, latency = await generate_narrative(
            backend, facts, retries)
    finally:
        if backend_override is None:
            await slots.release()

    subject = subject_line(facts)
    body = render(facts, narrative)

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            decision_id = await write_decision(
                signal_id=f"briefing-{session_date}", stage="SYSTEM",
                agent="A8", action="BRIEFING",
                payload={"session_date": session_date, "facts": facts,
                         "narrative": (narrative.model_dump()
                                       if narrative else None),
                         "slot": slot_name, "subject": subject},
                reason=subject, model_id=model_id, latency_ms=latency,
                conn=conn)
            cur = await conn.execute(
                """INSERT INTO journal.outbox
                     (kind, subject, body, fact_sheet, decision_id)
                   VALUES (%s,%s,%s,%s,%s) RETURNING message_id""",
                (KIND, subject, body, jb({k: facts[k] for k in
                                          ("session_date", "earnings", "ops")}),
                 decision_id))
            outbox_id = (await cur.fetchone())[0]

    log.info("briefing queued", extra=kv(
        outbox_id=outbox_id, decision_id=decision_id, slot=slot_name,
        narrative=narrative is not None, subject=subject))
    return outbox_id


async def main() -> None:
    from common.db import close_pool
    cfg = load_yaml(config_path("a8.yaml"))
    await register_config_version("a8 morning briefing run")
    try:
        await run_briefing(cfg)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
