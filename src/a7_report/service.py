"""A7 End-of-Day Trade Report (Phase 6) — oneshot, fired by a7-eod.timer
(16:35 ET / 15:35 CT on trading weekdays, after the C4 session-close pass).

Flow (models propose, code disposes — and here the model only decorates):
  1. code builds the fact sheet from the journal (facts.py)
  2. code picks a narrative model: HEAVY slot first (:8084 — started on
     demand if allowed, stopped afterwards if A7 started it: ownership rule,
     same as deadman blocks), else the resident ANALYST slot, else none
  3. narrative call is grammar-constrained; invalid after retry -> report
     ships WITHOUT narrative (the email is never blocked by an LLM)
  4. ONE TRANSACTION: SYSTEM/A7 REPORT decision row + journal.outbox insert
  5. the C5 mailer (separate dumb service, sole holder of SMTP credentials)
     picks the outbox row up within minutes

Idempotent per session date: if a REPORT decision for today already exists,
the run exits without writing (safe under timer retries / manual re-runs).
Non-session days (holidays) journal SKIPPED_NO_SESSION and send nothing —
the timer only fires Mon-Fri, the calendar check catches holidays.
"""
from __future__ import annotations

import asyncio
import shlex

import httpx

from common.config import config_path, load_yaml
from common.clock import utcnow
from common.db import get_pool, jb
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from a1_triage.backends import LlamaCppBackend
from a12_guard.wake import ensure_model_up, probe_health

from .facts import build_facts, report_window
from .narrative import (NarrativeValidationError, build_messages,
                        narrative_json_schema, validate_narrative)
from .render import render, subject_line

log = get_logger("a7.report")

KIND = "EOD_REPORT"


async def session_happened_today() -> bool:
    """Was there (or is there) an NYSE session on today's ET date?"""
    from zoneinfo import ZoneInfo
    import pandas_market_calendars as mcal
    day_key = (utcnow().astimezone(ZoneInfo("America/New_York"))
               .strftime("%Y-%m-%d"))
    nyse = mcal.get_calendar("NYSE")
    return not nyse.schedule(start_date=day_key, end_date=day_key).empty


async def already_reported(session_date: str) -> bool:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT 1 FROM journal.decisions
               WHERE stage='SYSTEM' AND agent='A7' AND action='REPORT'
                 AND payload->>'session_date' = %s LIMIT 1""", (session_date,))
        return (await cur.fetchone()) is not None


class SlotManager:
    """Resolve a narrative backend: heavy first (autostart + stop-after if WE
    started it), analyst fallback (wake via the existing a13 rule), else
    None. All commands come from config; probe-first, never blind-start."""

    def __init__(self, cfg: dict, probe=probe_health, runner=None):
        self.cfg = cfg
        self.probe = probe
        self.runner = runner or self._run_command
        self.started_heavy = False

    @staticmethod
    async def _run_command(command: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(command),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), timeout=30)
            return proc.returncode == 0
        except (OSError, asyncio.TimeoutError):
            return False

    async def acquire(self) -> tuple[LlamaCppBackend | None, str]:
        h = self.cfg.get("heavy") or {}
        if h:
            if await self.probe(h["endpoint"], float(h.get("probe_timeout_secs", 5))):
                return self._backend(h), "heavy"
            if h.get("autostart", False):
                log.info("starting heavy slot for report",
                         extra=kv(endpoint=h["endpoint"]))
                if await self.runner(str(h["start_command"])):
                    waited, poll = 0.0, float(h.get("poll_secs", 10))
                    while waited < float(h.get("ready_timeout_secs", 420)):
                        if await self.probe(h["endpoint"],
                                            float(h.get("probe_timeout_secs", 5))):
                            self.started_heavy = True
                            return self._backend(h), "heavy"
                        await asyncio.sleep(poll)
                        waited += poll
                    log.warning("heavy slot did not come up; falling back")

        f = self.cfg.get("analyst_fallback") or {}
        if f.get("enabled", True) and f.get("endpoint"):
            if await ensure_model_up(f["endpoint"], f.get("wake")):
                return self._backend(f), "analyst"
        return None, "none"

    async def release(self) -> None:
        """Stop the heavy slot ONLY if this run started it (ownership rule —
        an operator-started heavy session is not ours to kill)."""
        h = self.cfg.get("heavy") or {}
        if self.started_heavy and h.get("stop_after_use", True):
            log.info("stopping heavy slot (A7 started it)")
            await self.runner(str(h["stop_command"]))
            self.started_heavy = False

    def _backend(self, slot_cfg: dict) -> LlamaCppBackend:
        n = self.cfg.get("narrative") or {}
        return LlamaCppBackend({
            "endpoint": slot_cfg["endpoint"],
            "model_id": slot_cfg.get("model_id", "unknown"),
            "temperature": n.get("temperature", 0.0),
            "max_tokens": n.get("max_tokens", 900),
            "timeout_secs": n.get("timeout_secs", 300),
            # v0.12.4: per-slot thinking switch (heavy slot sets true — see
            # backends.py; A4/A5/A6-nightly/A7/A8 all build through here)
            "disable_thinking": slot_cfg.get("disable_thinking", False),
        })


async def generate_narrative(backend, facts: dict, retries: int):
    """Returns (NarrativeOutput | None, model_id | None, latency_ms)."""
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


async def run_report(cfg: dict, backend_override=None) -> int | None:
    """Build + journal + enqueue one EOD report. Returns outbox_id or None
    when skipped. backend_override: tests inject a stub and skip slots."""
    _, _, session_date = report_window()

    if await already_reported(session_date):
        log.info("report already exists — skipping",
                 extra=kv(session_date=session_date))
        return None

    if not await session_happened_today():
        if not (cfg.get("report") or {}).get("send_on_nonsession", False):
            await write_decision(
                signal_id=f"eod-{session_date}", stage="SYSTEM", agent="A7",
                action="SKIPPED_NO_SESSION",
                payload={"session_date": session_date},
                reason="no NYSE session today")
            log.info("no session today — skipped",
                     extra=kv(session_date=session_date))
            return None

    facts = await build_facts()

    slots = SlotManager(cfg)
    narrative = model_id = None
    latency = 0
    try:
        if backend_override is not None:
            backend, slot_name = backend_override, "stub"
        else:
            backend, slot_name = await slots.acquire()
        retries = int((cfg.get("narrative") or {}).get("retries_on_invalid", 1))
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
                signal_id=f"eod-{session_date}", stage="SYSTEM", agent="A7",
                action="REPORT",
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
                (KIND, subject, body, jb(facts), decision_id))
            outbox_id = (await cur.fetchone())[0]

    log.info("report queued", extra=kv(
        outbox_id=outbox_id, decision_id=decision_id, slot=slot_name,
        narrative=narrative is not None, subject=subject))
    return outbox_id


async def main() -> None:
    from common.db import close_pool
    cfg = load_yaml(config_path("a7.yaml"))
    await register_config_version("a7 eod report run")
    try:
        await run_report(cfg)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
