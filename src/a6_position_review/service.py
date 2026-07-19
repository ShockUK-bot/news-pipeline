"""A6 Position Review (Phase 8) — two oneshot entrypoints, both
recommendation-only (auto-apply OFF, the A12-v1 pattern: journal rows +
position_events, never orders; stops and exits stay with C4's code layers).

  eod      15:45 ET weekdays (a6-eod.timer): the baseline L6 overnight-hold
           check for the SHORT lane — remaining expected move vs. gap
           exposure, ONE analyst-slot call for the whole book. The heavy
           model NEVER runs during market hours (memory rule), so this mode
           probes/wakes the resident analyst slot only. No model -> journal
           SKIPPED_NO_MODEL; C4's code overnight rule still governs.

  nightly  20:00 ET weekdays (a6-nightly.timer): the deep pass — per
           position: thesis intact? evidence stale (the long lane's time
           stop)? were today's A12 guard actions sensible? Heavy slot via
           the shared SlotManager (analyst fallback). The code-side
           staleness rule journals STALE_FLAG rows BEFORE the model runs —
           a dead model cannot hide a stale position. Trim/exit/stale
           recommendations render into ONE alert email via journal.outbox
           (kind ALERT, C5 mails it); quiet nights journal only.

Both are idempotent per ET date via their anchor decisions (EOD_SHEET /
REVIEW), journal SKIPPED_NO_SESSION on non-sessions and SKIPPED_NO_POSITIONS
on an empty book (zero tokens either way). Every verdict decision row
carries the fact pack + verdict in payload; position_events mirrors it into
the position's flight recorder for A9/C6.

Run: python -m a6_position_review.service {eod|nightly}
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from common.config import config_path, load_yaml
from common.clock import utcnow
from common.db import get_pool, jb
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from a1_triage.backends import LlamaCppBackend
from a12_guard.wake import ensure_model_up
from a4_premarket.service import session_today
from a7_report.service import SlotManager
from c4_exec.state import position_event

from .context import build_pack, load_open_positions
from .prompt import build_eod_messages, build_review_messages
from .render import render_review_alert, subject_line
from .schema import (EOD_ACTION, REVIEW_ACTION, ReviewValidationError,
                     eod_json_schema, review_json_schema, validate_eod,
                     validate_review)

log = get_logger("a6.review")

ET = ZoneInfo("America/New_York")


async def anchor_exists(action: str, run_date: str) -> bool:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT 1 FROM journal.decisions
               WHERE stage='POSITION_REVIEW' AND agent='A6' AND action=%s
                 AND payload->>'run_date' = %s LIMIT 1""",
            (action, run_date))
        return (await cur.fetchone()) is not None


async def _skip(action: str, run_date: str, reason: str, **payload) -> None:
    await write_decision(
        signal_id=f"posrev-{run_date}", stage="POSITION_REVIEW", agent="A6",
        action=action, payload={"run_date": run_date, **payload},
        reason=reason)
    log.info("skipped", extra=kv(action=action, reason=reason))


# --------------------------------------------------------------------------
# EOD overnight-hold check (analyst slot only — market hours)
# --------------------------------------------------------------------------

async def eod_backend(cfg: dict):
    e = cfg.get("eod") or {}
    if not e.get("endpoint"):
        return None
    if not await ensure_model_up(e["endpoint"], e.get("wake")):
        return None
    return LlamaCppBackend({
        "endpoint": e["endpoint"],
        "model_id": e.get("model_id", "unknown"),
        "temperature": float(e.get("temperature", 0.0)),
        "max_tokens": int(e.get("max_tokens", 900)),
        "timeout_secs": float(e.get("timeout_secs", 180)),
    })


async def run_eod(cfg: dict, backend_override=None,
                  now: datetime | None = None) -> int:
    """Returns the number of positions reviewed (0 when skipped)."""
    now = now or utcnow()
    run_date = now.astimezone(ET).strftime("%Y-%m-%d")
    rcfg = cfg.get("review") or {}
    stale_weeks = float(rcfg.get("stale_weeks", 4))

    if await anchor_exists("EOD_SHEET", run_date):
        log.info("eod sheet already exists — skipping",
                 extra=kv(date=run_date))
        return 0
    if not session_today(now) and not (cfg.get("report") or {}).get(
            "send_on_nonsession", False):
        await _skip("SKIPPED_NO_SESSION", run_date, "no NYSE session today",
                    lane="eod")
        return 0

    positions = await load_open_positions(horizon="SHORT")
    positions = positions[:int(rcfg.get("max_positions", 20))]
    if not positions:
        await _skip("SKIPPED_NO_POSITIONS", run_date,
                    "no open SHORT-lane positions at the EOD check",
                    lane="eod")
        return 0

    packs = [await build_pack(p, now, stale_weeks) for p in positions]
    backend = backend_override or await eod_backend(cfg)
    if backend is None:
        await _skip("SKIPPED_NO_MODEL", run_date,
                    "analyst slot unreachable — C4's code overnight rule "
                    "governs unaided", lane="eod",
                    positions=[p["position_id"] for p in packs])
        return 0

    retries = int((cfg.get("eod") or {}).get("retries_on_invalid", 1))
    sheet = None
    error: ReviewValidationError | None = None
    model_id = None
    latency = 0
    for _ in range(1 + retries):
        messages = build_eod_messages(
            packs, retry_error=error.detail if error else None)
        try:
            reply = await backend.complete(messages, eod_json_schema())
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            log.warning("eod model call failed", extra=kv(error=repr(e)[:200]))
            break
        latency += reply.latency_ms
        model_id = reply.model_id
        try:
            sheet = validate_eod(reply.text)
            break
        except ReviewValidationError as e:
            error = e
            log.warning("invalid eod output", extra=kv(detail=e.detail[:150]))
    if sheet is None:
        await _skip("SKIPPED_NO_MODEL", run_date,
                    "eod model output unavailable/invalid — C4's code "
                    "overnight rule governs unaided", lane="eod",
                    positions=[p["position_id"] for p in packs])
        return 0

    by_id = {v.position_id: v for v in sheet.verdicts}
    pool = await get_pool()
    results = []
    for pack in packs:
        v = by_id.get(pack["position_id"])
        if v is None:
            verdict, confidence = "hold_overnight", 0.0
            rationale = "model omitted this position — default hold (code)"
        else:
            verdict, confidence, rationale = v.verdict, v.confidence, v.rationale
        action = EOD_ACTION[verdict]
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=f"posrev-{run_date}:{pack['position_id']}",
                    stage="POSITION_REVIEW", agent="A6", action=action,
                    ticker=pack["ticker"],
                    payload={"run_date": run_date, "lane": "eod",
                             "verdict": verdict, "rationale": rationale,
                             "pack": pack},
                    reason=rationale, confidence=confidence,
                    model_id=model_id, conn=conn)
                await position_event(
                    pack["position_id"], "OVERNIGHT_HOLD_DECISION", "A6",
                    new_value={"verdict": verdict, "rationale": rationale},
                    r_progress=pack["r_progress"],
                    detail="A6 EOD overnight-hold check (recommendation)",
                    decision_id=decision_id, conn=conn)
        results.append({"position_id": pack["position_id"],
                        "ticker": pack["ticker"], "verdict": verdict})

    await write_decision(
        signal_id=f"posrev-{run_date}", stage="POSITION_REVIEW", agent="A6",
        action="EOD_SHEET",
        payload={"run_date": run_date, "reviewed": len(results),
                 "verdicts": results,
                 "exit_recos": sum(1 for r in results
                                   if r["verdict"] == "exit_before_close")},
        reason=f"EOD overnight-hold check: {len(results)} SHORT position"
               f"{'s' if len(results) != 1 else ''} reviewed",
        model_id=model_id, latency_ms=latency)
    log.info("eod check done", extra=kv(reviewed=len(results)))
    return len(results)


# --------------------------------------------------------------------------
# nightly deep review (heavy slot via SlotManager)
# --------------------------------------------------------------------------

async def review_position(backend, pack: dict, retries: int):
    """Returns (ReviewVerdict | None, model_id | None, latency_ms)."""
    error: ReviewValidationError | None = None
    total = 0
    model_id = None
    for _ in range(1 + retries):
        messages = build_review_messages(
            pack, retry_error=error.detail if error else None)
        try:
            reply = await backend.complete(messages, review_json_schema())
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            log.warning("review call failed",
                        extra=kv(position_id=pack["position_id"],
                                 error=repr(e)[:200]))
            return None, model_id, total
        total += reply.latency_ms
        model_id = reply.model_id
        try:
            return validate_review(reply.text), model_id, total
        except ReviewValidationError as e:
            error = e
            log.warning("invalid review output",
                        extra=kv(position_id=pack["position_id"],
                                 detail=e.detail[:150]))
    return None, model_id, total


async def run_nightly(cfg: dict, backend_override=None,
                      now: datetime | None = None) -> int | None:
    """Returns the alert's outbox message_id (None when skipped, quiet, or
    email disabled)."""
    now = now or utcnow()
    run_date = now.astimezone(ET).strftime("%Y-%m-%d")
    rcfg = cfg.get("review") or {}
    stale_weeks = float(rcfg.get("stale_weeks", 4))

    if await anchor_exists("REVIEW", run_date):
        log.info("review already exists — skipping", extra=kv(date=run_date))
        return None
    if not session_today(now) and not (cfg.get("report") or {}).get(
            "send_on_nonsession", False):
        await _skip("SKIPPED_NO_SESSION", run_date, "no NYSE session today",
                    lane="nightly")
        return None

    positions = await load_open_positions()
    positions = positions[:int(rcfg.get("max_positions", 20))]
    if not positions:
        await _skip("SKIPPED_NO_POSITIONS", run_date,
                    "no open positions to review", lane="nightly")
        return None

    pool = await get_pool()
    slots = SlotManager(cfg)
    outbox_id: int | None = None
    try:
        if backend_override is not None:
            backend, slot_name = backend_override, "stub"
        else:
            backend, slot_name = await slots.acquire()

        recos: list[dict] = []
        holds: list[dict] = []
        stale_flagged = 0
        reviewed = 0
        rejected = 0
        model_id = None
        total_latency = 0
        retries = int((cfg.get("narrative") or {})
                      .get("retries_on_invalid", 1))

        for pos in positions:
            pack = await build_pack(pos, now, stale_weeks)

            # code rule first: a dead model cannot hide a stale position
            if pack["staleness_code"] == "stale":
                async with pool.connection() as conn:
                    async with conn.transaction():
                        decision_id = await write_decision(
                            signal_id=f"posrev-{run_date}:{pack['position_id']}",
                            stage="POSITION_REVIEW", agent="A6",
                            action="STALE_FLAG", ticker=pack["ticker"],
                            payload={"run_date": run_date,
                                     "news_recency": pack["news_recency"],
                                     "stale_weeks": stale_weeks,
                                     "opened_days_ago": pack["opened_days_ago"]},
                            reason=f"no escalated news on {pack['ticker']} in "
                                   f"{stale_weeks:.0f}+ weeks — long-lane "
                                   "staleness rule (code)", conn=conn)
                        await position_event(
                            pack["position_id"], "STALE_FLAG", "A6",
                            new_value={"stale_weeks": stale_weeks},
                            r_progress=pack["r_progress"],
                            detail="evidence staleness (code rule)",
                            decision_id=decision_id, conn=conn)
                stale_flagged += 1
                recos.append({**pack, "action": "STALE",
                              "rationale": "staleness rule: no confirming "
                                           "evidence — review for exit"})

            if backend is None:
                continue
            verdict, vid, lat = await review_position(backend, pack, retries)
            model_id = vid or model_id
            total_latency += lat
            if verdict is None:
                await write_decision(
                    signal_id=f"posrev-{run_date}:{pack['position_id']}",
                    stage="POSITION_REVIEW", agent="A6",
                    action="REVIEW_REJECT", ticker=pack["ticker"],
                    payload={"run_date": run_date, "slot": slot_name},
                    reason="model output invalid/unavailable for this "
                           "position", model_id=model_id)
                rejected += 1
                continue
            action = REVIEW_ACTION[verdict.verdict]
            async with pool.connection() as conn:
                async with conn.transaction():
                    decision_id = await write_decision(
                        signal_id=f"posrev-{run_date}:{pack['position_id']}",
                        stage="POSITION_REVIEW", agent="A6", action=action,
                        ticker=pack["ticker"],
                        payload={"run_date": run_date, "lane": "nightly",
                                 **verdict.model_dump(), "pack": pack},
                        reason=verdict.rationale,
                        confidence=verdict.confidence,
                        model_id=model_id, conn=conn)
                    await position_event(
                        pack["position_id"], "POSITION_REVIEW", "A6",
                        new_value=verdict.model_dump(),
                        r_progress=pack["r_progress"],
                        detail="A6 nightly review (recommendation)",
                        decision_id=decision_id, conn=conn)
            reviewed += 1
            entry = {**pack, "action": action, "rationale": verdict.rationale}
            if verdict.verdict in ("trim", "exit"):
                recos.append(entry)
            else:
                holds.append(entry)

        if backend is None and not stale_flagged:
            await _skip("SKIPPED_NO_MODEL", run_date,
                        "no model slot available and no code-rule flags — "
                        "nothing to report", lane="nightly")
            return None

        stats = {"run_date": run_date, "reviewed": reviewed,
                 "rejected": rejected, "stale_flagged": stale_flagged,
                 "recommendations": len(recos), "holds": len(holds),
                 "slot": slot_name}
        email = bool((cfg.get("alert") or {}).get("email", True))
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=f"posrev-{run_date}", stage="POSITION_REVIEW",
                    agent="A6", action="REVIEW",
                    payload={**stats,
                             "recos": [{k: r.get(k) for k in
                                        ("position_id", "ticker", "action",
                                         "rationale")} for r in recos]},
                    reason=f"nightly review: {reviewed} reviewed, "
                           f"{len(recos)} recommendation"
                           f"{'s' if len(recos) != 1 else ''}",
                    model_id=model_id, latency_ms=total_latency, conn=conn)
                if recos and email:
                    cur = await conn.execute(
                        """INSERT INTO journal.outbox
                             (kind, subject, body, fact_sheet, decision_id)
                           VALUES ('ALERT',%s,%s,%s,%s)
                           RETURNING message_id""",
                        (subject_line(run_date, recos),
                         render_review_alert(run_date, recos, holds, stats),
                         jb(stats), decision_id))
                    outbox_id = (await cur.fetchone())[0]

        log.info("nightly review done",
                 extra=kv(**{**stats, "outbox_id": outbox_id}))
        return outbox_id
    finally:
        if backend_override is None:
            await slots.release()


async def main() -> None:
    from common.db import close_pool
    mode = sys.argv[1] if len(sys.argv) > 1 else "nightly"
    if mode not in ("eod", "nightly"):
        raise SystemExit(f"unknown mode {mode!r} — use: eod | nightly")
    cfg = load_yaml(config_path("a6.yaml"))
    await register_config_version(f"a6 position review run ({mode})")
    try:
        if mode == "eod":
            await run_eod(cfg)
        else:
            await run_nightly(cfg)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
