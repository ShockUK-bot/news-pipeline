"""A4 Pre-Market Review (Phase 7) — oneshot, fired by a4-premarket.timer at
07:00 ET on weekdays. The consumer of `signal.overnight` (accumulating since
Phase 2 — this agent finally reads it).

Flow (models rank, code routes — the gates are unchanged and un-bypassed):
  1. BULK EXPIRY (one SQL pass): overnight messages older than
     sheet.max_age_hours are marked done and journaled as ONE
     PREMARKET/EXPIRED_BULK summary row — this is how the multi-week backlog
     drains on first start without journal spam or tokens.
  2. Claim the fresh messages. Code routes FIRST, before any model call:
       * tickers intersect an open position -> signal.guard (priority 0,
         A12) — capital protection is not a ranking decision;
       * no tickers -> signal.thesis (A5 lane, Phase 8 consumer).
  3. The remaining candidates (top-K by queue priority) go to the model in
     ONE grammar-constrained call -> ActionSheet (lane + rank + rationale
     per item). Heavy slot first via the shared SlotManager (started on
     demand at 07:00, stopped after — well before the open), analyst
     fallback, else a deterministic priority-ranked fallback sheet. The
     briefing always exists; only its intelligence degrades.
  4. Disposition per item in its own transaction, then ack:
       * open_candidate -> re-enqueue the ORIGINAL TriagedSignal payload on
         signal.analyst with available_ts = first session open + blackout
         (queue-native delayed delivery). A2 then evaluates against LIVE
         opening prices and C3's open-handoff rule does the gap math
         (priced-in -> veto; small gap on rated news -> the opportunity).
       * thesis -> signal.thesis;  ignore -> journaled, no route.
  5. One PREMARKET/SHEET decision + a MORNING_BRIEFING outbox row — the C5
     mailer emails the ranked sheet minutes later.

Idempotent per session date (SHEET decision check, same as A7); item-level
redelivery is a no-op via enqueue dedup keys. Non-session days journal
SKIPPED_NO_SESSION and exit.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from common.config import config_path, load_yaml
from common.clock import utcnow
from common.db import get_pool, jb
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from common.queue import ack, claim, enqueue, fail
from router.facts import open_position_ids
from a7_report.service import SlotManager

from .prompt import build_messages
from .schema import (ActionSheet, SheetItem, SheetValidationError,
                     sheet_json_schema, validate_sheet)
from .render import render_briefing, subject_line

log = get_logger("a4.premarket")

IN_QUEUE = "signal.overnight"
ANALYST_QUEUE = "signal.analyst"
GUARD_QUEUE = "signal.guard"
THESIS_QUEUE = "signal.thesis"
CONSUMER = "a4-premarket"
ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------
# session / timing helpers
# --------------------------------------------------------------------------

def session_today(now: datetime | None = None) -> bool:
    import pandas_market_calendars as mcal
    now = now or utcnow()
    day = now.astimezone(ET).strftime("%Y-%m-%d")
    return not mcal.get_calendar("NYSE").schedule(start_date=day,
                                                  end_date=day).empty


def next_entry_ts(now: datetime | None = None, blackout_min: int = 15) -> datetime:
    """First moment entries are allowed: the next session open + blackout
    (holiday-aware; if we're already past it, now)."""
    import pandas_market_calendars as mcal
    now = now or utcnow()
    start = now.astimezone(ET).strftime("%Y-%m-%d")
    end = (now.astimezone(ET) + timedelta(days=8)).strftime("%Y-%m-%d")
    sched = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end)
    for _, row in sched.iterrows():
        open_ts = row["market_open"].to_pydatetime()
        close_ts = row["market_close"].to_pydatetime()
        if close_ts > now:
            entry = open_ts + timedelta(minutes=blackout_min)
            return max(entry, now)
    return now                                       # calendar gap: fail open


# --------------------------------------------------------------------------
# queue helpers
# --------------------------------------------------------------------------

async def bulk_expire(cutoff: datetime) -> int:
    """One SQL pass: retire stale overnight messages (no per-item journal
    rows, no tokens). Returns the count."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """UPDATE queue.messages SET done_ts = now()
               WHERE queue_name = %s AND done_ts IS NULL
                 AND claimed_ts IS NULL AND enqueued_ts < %s""",
            (IN_QUEUE, cutoff))
        return cur.rowcount or 0


async def enqueue_delayed(queue_name: str, dedup_key: str, payload: dict,
                          priority: int, available_ts: datetime,
                          conn=None) -> bool:
    """enqueue() plus available_ts — queue-native delayed delivery (the
    schema was built for exactly this; common.queue stays untouched)."""
    sql = """INSERT INTO queue.messages
               (queue_name, dedup_key, priority, payload, available_ts)
             VALUES (%s,%s,%s,%s,%s)
             ON CONFLICT (queue_name, dedup_key) DO NOTHING
             RETURNING msg_id"""
    channel = "q_" + queue_name.replace(".", "_")

    async def _run(c) -> bool:
        cur = await c.execute(sql, (queue_name, dedup_key, priority,
                                    jb(payload), available_ts))
        if await cur.fetchone() is not None:
            await c.execute(f"NOTIFY {channel}")
            return True
        return False

    if conn is not None:
        return await _run(conn)
    pool = await get_pool()
    async with pool.connection() as c:
        return await _run(c)


async def fetch_headline(item_id: str, revision: int) -> dict:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT headline, source, source_tier, is_correction, received_ts
               FROM news.news_items WHERE item_id=%s AND revision=%s""",
            (item_id, revision))
        row = await cur.fetchone()
        if row is None:
            return {}
        return {"headline": row[0], "source": row[1], "source_tier": row[2],
                "is_correction": row[3], "received_ts": row[4]}


async def sheet_already_done(session_date: str) -> bool:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT 1 FROM journal.decisions
               WHERE stage='PREMARKET' AND agent='A4' AND action='SHEET'
                 AND payload->>'session_date' = %s LIMIT 1""", (session_date,))
        return (await cur.fetchone()) is not None


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def fallback_sheet(candidates: list[dict], open_k: int) -> ActionSheet:
    """Deterministic ranking when no model is reachable: top-K by queue
    priority become open candidates in priority order, the rest ignore."""
    items = []
    for i, c in enumerate(candidates):
        lane = "open_candidate" if i < open_k else "ignore"
        items.append(SheetItem(item_id=c["item_id"], lane=lane,
                               rank=min(i + 1, 50),
                               rationale="deterministic fallback: queue "
                                         "priority ordering (model offline)"))
    return ActionSheet(items=items,
                       summary=f"Deterministic fallback sheet — model "
                               f"unavailable. {len(candidates)} fresh items; "
                               f"top {min(open_k, len(candidates))} forwarded "
                               f"to the open by priority.")


async def rank_with_model(backend, candidates: list[dict], retries: int):
    """Returns (ActionSheet | None, model_id | None, latency_ms)."""
    if backend is None or not candidates:
        return None, None, 0
    prompt_items = [{k: c[k] for k in
                     ("item_id", "headline", "tickers", "source_tier",
                      "independent_outlets", "urgency", "novelty_score",
                      "direction_hint", "is_correction", "age_hours")}
                    for c in candidates]
    error: SheetValidationError | None = None
    total = 0
    for _ in range(1 + retries):
        messages = build_messages(prompt_items,
                                  retry_error=error.detail if error else None)
        try:
            reply = await backend.complete(messages, sheet_json_schema())
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            log.warning("sheet model call failed", extra=kv(error=repr(e)[:200]))
            return None, None, total
        total += reply.latency_ms
        try:
            return validate_sheet(reply.text), reply.model_id, total
        except SheetValidationError as e:
            error = e
            log.warning("invalid sheet output", extra=kv(detail=e.detail[:150]))
    return None, backend.model_id, total


async def run_premarket(cfg: dict, backend_override=None,
                        now: datetime | None = None) -> int | None:
    """Returns the sheet email's outbox message_id — None when skipped OR
    when report.email is false (the v0.11.0 default: A8 sends the ONE
    consolidated morning email; the SHEET decision is still written)."""
    now = now or utcnow()
    session_date = now.astimezone(ET).strftime("%Y-%m-%d")
    scfg = cfg.get("sheet") or {}
    max_age_h = float(scfg.get("max_age_hours", 72))
    top_k = int(scfg.get("top_k", 15))
    open_k = int(scfg.get("fallback_open_k", 5))
    blackout = int(scfg.get("blackout_min", 15))
    batch_max = int(scfg.get("batch_max", 300))

    if await sheet_already_done(session_date):
        log.info("sheet already exists — skipping", extra=kv(date=session_date))
        return None
    if not session_today(now) and not (cfg.get("report") or {}).get(
            "send_on_nonsession", False):
        await write_decision(
            signal_id=f"premarket-{session_date}", stage="PREMARKET",
            agent="A4", action="SKIPPED_NO_SESSION",
            payload={"session_date": session_date},
            reason="no NYSE session today")
        return None

    # 1. bulk expiry ---------------------------------------------------------
    expired = await bulk_expire(now - timedelta(hours=max_age_h))
    if expired:
        await write_decision(
            signal_id=f"premarket-{session_date}", stage="PREMARKET",
            agent="A4", action="EXPIRED_BULK",
            payload={"session_date": session_date, "expired_count": expired,
                     "max_age_hours": max_age_h},
            reason=f"bulk-expired {expired} overnight messages older than "
                   f"{max_age_h:.0f}h")
        log.info("bulk expired", extra=kv(count=expired))

    # 2. claim fresh messages, code-route guard/thesis -----------------------
    entry_ts = next_entry_ts(now, blackout)
    claimed: list = []
    while len(claimed) < batch_max:
        msg = await claim(IN_QUEUE, CONSUMER)
        if msg is None:
            break
        claimed.append(msg)

    candidates: list[dict] = []      # meta dicts, model input
    by_msg: dict[int, object] = {}   # msg_id -> claimed msg
    routed_guard: list[dict] = []
    routed_thesis: list[dict] = []
    pool = await get_pool()

    for msg in claimed:
        body = msg.payload.get("body") or {}
        item_ref = body.get("item_ref") or {}
        triage = body.get("triage") or {}
        item_id = item_ref.get("item_id")
        revision = int(item_ref.get("revision") or 1)
        if not item_id:
            await fail(msg.msg_id, "malformed overnight signal")
            continue
        meta = await fetch_headline(item_id, revision)
        tickers = triage.get("tickers") or []

        pos_ids = await open_position_ids(tickers)
        if pos_ids:
            async with pool.connection() as conn:
                async with conn.transaction():
                    await write_decision(
                        signal_id=item_id, item_id=item_id,
                        item_revision=revision, ticker=tickers[0],
                        stage="PREMARKET", agent="A4", action="GUARD",
                        payload={"position_ids": pos_ids,
                                 "headline": meta.get("headline")},
                        reason="overnight item touches an open position — "
                               "routed to A12", conn=conn)
                    await enqueue(GUARD_QUEUE, f"{item_id}:{revision}:a4guard",
                                  msg.payload, priority=0, conn=conn)
            await ack(msg.msg_id)
            routed_guard.append({"item_id": item_id,
                                 "headline": meta.get("headline"),
                                 "tickers": tickers})
            continue

        if not tickers:
            async with pool.connection() as conn:
                async with conn.transaction():
                    await write_decision(
                        signal_id=item_id, item_id=item_id,
                        item_revision=revision,
                        stage="PREMARKET", agent="A4", action="THESIS",
                        payload={"headline": meta.get("headline")},
                        reason="material, no mappable ticker — thesis lane",
                        conn=conn)
                    await enqueue(THESIS_QUEUE, f"{item_id}:{revision}:a4thesis",
                                  msg.payload, priority=100, conn=conn)
            await ack(msg.msg_id)
            routed_thesis.append({"item_id": item_id,
                                  "headline": meta.get("headline")})
            continue

        received = meta.get("received_ts")
        age_h = (round((now - received).total_seconds() / 3600.0, 1)
                 if received else None)
        candidates.append({
            "item_id": item_id, "revision": revision, "msg_id": msg.msg_id,
            "headline": meta.get("headline"), "tickers": tickers,
            "source_tier": meta.get("source_tier"),
            "is_correction": bool(meta.get("is_correction")),
            "independent_outlets": int((body.get("routing") or {})
                                       .get("independent_outlets", 1) or 1),
            "urgency": triage.get("urgency"),
            "novelty_score": triage.get("novelty_score"),
            "direction_hint": triage.get("direction_hint"),
            "age_hours": age_h, "priority": msg.priority,
        })
        by_msg[msg.msg_id] = msg

    candidates.sort(key=lambda c: c["priority"])
    model_pool = candidates[:top_k]
    overflow = candidates[top_k:]

    # 3. model ranking (heavy -> analyst -> deterministic fallback) ----------
    slots = SlotManager(cfg)
    sheet = model_id = None
    latency = 0
    slot_name = "none"
    try:
        if backend_override is not None:
            backend, slot_name = backend_override, "stub"
        elif model_pool:
            backend, slot_name = await slots.acquire()
        else:
            backend = None
        retries = int((cfg.get("narrative") or {}).get("retries_on_invalid", 1))
        sheet, model_id, latency = await rank_with_model(backend, model_pool,
                                                         retries)
    finally:
        if backend_override is None:
            await slots.release()
    if sheet is None and model_pool:
        sheet, slot_name = fallback_sheet(model_pool, open_k), "fallback"
    elif sheet is None:
        sheet = ActionSheet(items=[], summary="Quiet night — no fresh "
                                              "candidates in the overnight "
                                              "queue.")

    lanes = {s.item_id: s for s in sheet.items}

    # 4. dispositions --------------------------------------------------------
    open_forwarded: list[dict] = []
    thesis_from_model: list[dict] = []
    ignored = 0
    for c in model_pool + overflow:
        verdict = lanes.get(c["item_id"])
        lane = verdict.lane if verdict else "ignore"
        rank = verdict.rank if verdict else 50
        rationale = (verdict.rationale if verdict
                     else "below top-K cutoff — not model-ranked")
        msg = by_msg[c["msg_id"]]
        dedup = f"{c['item_id']}:{c['revision']}"
        async with pool.connection() as conn:
            async with conn.transaction():
                await write_decision(
                    signal_id=c["item_id"], item_id=c["item_id"],
                    item_revision=c["revision"], ticker=c["tickers"][0],
                    stage="PREMARKET", agent="A4",
                    action={"open_candidate": "OPEN_CANDIDATE",
                            "thesis": "THESIS", "ignore": "IGNORE"}[lane],
                    payload={"lane": lane, "rank": rank,
                             "rationale": rationale,
                             "headline": c["headline"],
                             "entry_ts": entry_ts.isoformat(),
                             "session_date": session_date},
                    reason=rationale, model_id=model_id if verdict else None,
                    conn=conn)
                if lane == "open_candidate":
                    await enqueue_delayed(
                        ANALYST_QUEUE, f"{dedup}:handoff", msg.payload,
                        priority=40 + min(rank, 50), available_ts=entry_ts,
                        conn=conn)
                elif lane == "thesis":
                    await enqueue(THESIS_QUEUE, f"{dedup}:a4thesis",
                                  msg.payload, priority=100, conn=conn)
        await ack(msg.msg_id)
        if lane == "open_candidate":
            open_forwarded.append({**c, "rank": rank, "rationale": rationale})
        elif lane == "thesis":
            thesis_from_model.append(c)
        else:
            ignored += 1

    open_forwarded.sort(key=lambda c: c["rank"])

    # 5. sheet decision + briefing email -------------------------------------
    stats = {"session_date": session_date, "expired_bulk": expired,
             "fresh": len(claimed), "open_candidates": len(open_forwarded),
             "guard_routed": len(routed_guard),
             "thesis_routed": len(routed_thesis) + len(thesis_from_model),
             "ignored": ignored, "slot": slot_name,
             "entry_ts": entry_ts.isoformat()}
    subject = subject_line(session_date, stats)
    body_text = render_briefing(session_date, sheet.summary, open_forwarded,
                                routed_guard,
                                routed_thesis + thesis_from_model,
                                stats, slot_name)
    # v0.11.0: report.email gates the standalone sheet email. Default False —
    # A8 (Phase 9, 07:35 ET) embeds this sheet in the ONE consolidated
    # morning briefing. The SHEET decision (A8's source, and the idempotency
    # anchor) is written regardless. Set report.email: true to restore the
    # pre-Phase-9 separate email.
    send_email = bool((cfg.get("report") or {}).get("email", False))
    outbox_id = None
    async with pool.connection() as conn:
        async with conn.transaction():
            decision_id = await write_decision(
                signal_id=f"premarket-{session_date}", stage="PREMARKET",
                agent="A4", action="SHEET",
                payload={**stats,
                         "sheet": sheet.model_dump(),
                         "open_forwarded": [
                             {k: c[k] for k in ("item_id", "tickers", "rank")}
                             for c in open_forwarded]},
                reason=sheet.summary, model_id=model_id, latency_ms=latency,
                conn=conn)
            if send_email:
                cur = await conn.execute(
                    """INSERT INTO journal.outbox
                         (kind, subject, body, fact_sheet, decision_id)
                       VALUES ('MORNING_BRIEFING',%s,%s,%s,%s)
                       RETURNING message_id""",
                    (subject, body_text, jb(stats), decision_id))
                outbox_id = (await cur.fetchone())[0]

    log.info("premarket sheet done", extra=kv(**{**stats,
                                                 "outbox_id": outbox_id}))
    return outbox_id


async def main() -> None:
    from common.db import close_pool
    cfg = load_yaml(config_path("a4.yaml"))
    await register_config_version("a4 premarket run")
    try:
        await run_premarket(cfg)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
