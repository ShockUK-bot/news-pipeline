"""A4 late pass (v0.12.13; budget pacing v0.12.15; quiet exhaustion
v0.12.16) — oneshot, fired by
a4-late.timer every 10 minutes between 07:00 and 09:59 ET on weekdays.
Closes the pre-market blind window: before v0.12.13, `signal.overnight` was
drained exactly once per day (the 07:00 ET sheet run), so news breaking
between the sheet and the 09:30 open sat unread until the NEXT morning and
died of staleness (2026-08-04: the PLTR "analysts turning bullish" item,
escalated 07:46 ET, was never seen that day).

v0.12.15 — the budget is PACED, not first-come-first-served. On the first
busy earnings morning (2026-08-05) the v0.12.13 daily budget of 10 was
consumed entirely by the 07:10 ET pass; ~140 later pre-market signals
(GOOG, LLY, AZN, MRVL, SHOP among them) were IGNOREd over-budget — the
PLTR pattern reborn one layer up. Now each pass may forward only its
allowance = min(late_pass_max, ceil(remaining_budget / remaining_passes)),
so budget survives to the final pre-open pass by construction, and items
that lose a pass are DEFERRED back to the queue (attempt refunded) to
compete again next pass instead of being discarded.

Flow (deterministic, NO model call — A2/C3 remain the judges at the open):
  1. Guards: exit silently unless (a) today's SHEET decision exists (the
     07:00 run owns the bulk drain and ranking; the late pass never
     front-runs it), and (b) the session hasn't opened yet (once the market
     is open, router rule 4 sends new items straight to signal.analyst, so
     there is nothing for this pass to do).
  2. Claim fresh + previously-deferred overnight messages. Code-routes
     first, identical to the sheet run: open-position tickers ->
     signal.guard (priority 0); no tickers -> signal.thesis (A5's lane).
  3. Remaining candidates, ordered by queue priority (which encodes A1's
     priority_score): the top `allowance` are re-enqueued on signal.analyst
     with available_ts = open + blackout (the same queue-native delayed
     open-handoff the sheet's open_candidates use, same dedup key, so an
     item can never be forwarded twice) and journaled as
     PREMARKET/LATE_CANDIDATE. ALL other candidates are deferred back to
     the queue (visible again in ~9 minutes, attempt refunded — repeated
     deferral can never DLQ them): while budget remains they compete
     again next pass; whatever is still deferred at the open stays on
     signal.overnight and is ranked — and journaled — by tomorrow's
     07:00 sheet like any other overnight item (or bulk-expired at
     max_age_hours). v0.12.16: exhaustion no longer journals a same-day
     IGNORE per item — on 2026-08-06 the moment the 24th slot was spent,
     ~100 pool items hit the tape as one IGNORE burst in a single
     minute, which read as a malfunction; the sheet's next-morning
     ranking is the honest (and quieter) disposition.

The daily budget keeps the open-handoff token spend bounded: the sheet run
forwards at most top_k ranked candidates; late passes add at most
late_daily_max more across the whole window. Thresholds, gates, and risk
checks are untouched — this release only guarantees fresh signals REACH them.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta

from common.config import config_path, load_yaml
from common.clock import utcnow
from common.db import get_pool
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from common.queue import ack, claim, defer, enqueue, fail

from .service import (ANALYST_QUEUE, ET, GUARD_QUEUE, IN_QUEUE,
                      THESIS_QUEUE, enqueue_delayed, fetch_headline,
                      next_entry_ts, sheet_already_done)
from router.facts import open_position_ids

log = get_logger("a4.late")

LATE_CONSUMER = "a4-late"

# The timer fires every 10 minutes; deferred items become visible again
# shortly before the next firing so they are always in the next pass's pool.
PASS_CADENCE_MIN = 10
DEFER_SECS = 540


# --------------------------------------------------------------------------
# pure helpers (unit-tested without a database)
# --------------------------------------------------------------------------

def open_ts_from_entry(entry_ts: datetime, blackout_min: int) -> datetime:
    """The session open implied by next_entry_ts()'s entry moment."""
    return entry_ts - timedelta(minutes=blackout_min)


def should_run(now: datetime, sheet_done: bool, open_ts: datetime) -> bool:
    """The late pass runs only in the window AFTER the daily sheet exists and
    BEFORE the open — and only when that open is TODAY'S open. The same-day
    check makes an out-of-window invocation (manual `systemctl start` in the
    evening, a mis-set timer) a guaranteed no-op instead of a premature drain
    of the overnight queue toward tomorrow's open, which would bypass the
    07:00 sheet's ranking."""
    same_session = (open_ts.astimezone(ET).date() == now.astimezone(ET).date())
    return sheet_done and same_session and now < open_ts


def passes_remaining(now: datetime, open_ts: datetime,
                     cadence_min: int = PASS_CADENCE_MIN) -> int:
    """Timer firings left before the open, INCLUDING the current one.
    Never less than 1 (the caller only runs pre-open)."""
    if now >= open_ts:
        return 1
    gap_min = (open_ts - now).total_seconds() / 60.0
    return int(gap_min // cadence_min) + 1


def plan_forwarding(candidates: list[dict], already_forwarded: int,
                    late_daily_max: int, late_pass_max: int = 4,
                    remaining_passes: int = 1,
                    ) -> tuple[list[dict], list[dict], int]:
    """Split candidates (any order) into (forward, rest) plus this pass's
    allowance. Forwarding order is queue priority ascending — lower is more
    urgent, matching queue.claim_next — with msg_id (FIFO) as tie-break.

    v0.12.15 pacing: allowance = min(late_pass_max,
    ceil(remaining_budget / remaining_passes)). Spending exactly the
    allowance each pass leaves at least one budget slot for every remaining
    pass down to the last one before the open, so an early flood can no
    longer consume the whole day (2026-08-05: budget of 10 gone at the
    first 07:10 ET pass; ~140 later signals dropped unmeasured)."""
    budget = max(0, late_daily_max - already_forwarded)
    if budget == 0:
        allowance = 0
    else:
        allowance = min(late_pass_max, budget,
                        math.ceil(budget / max(1, remaining_passes)))
    ordered = sorted(candidates, key=lambda c: (c["priority"], c["msg_id"]))
    return ordered[:allowance], ordered[allowance:], allowance


# --------------------------------------------------------------------------
# db helpers
# --------------------------------------------------------------------------

async def late_forwarded_today(session_date: str) -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT count(*) FROM journal.decisions
               WHERE stage='PREMARKET' AND agent='A4'
                 AND action='LATE_CANDIDATE'
                 AND payload->>'session_date' = %s""", (session_date,))
        return int((await cur.fetchone())[0])


# --------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------

async def run_late_pass(cfg: dict, now: datetime | None = None) -> dict:
    """Returns a stats dict (also logged); {'ran': False, ...} on no-op."""
    now = now or utcnow()
    session_date = now.astimezone(ET).strftime("%Y-%m-%d")
    scfg = cfg.get("sheet") or {}
    blackout = int(scfg.get("blackout_min", 15))
    batch_max = int(scfg.get("batch_max", 300))
    late_daily_max = int(scfg.get("late_daily_max", 24))
    late_pass_max = int(scfg.get("late_pass_max", 4))

    entry_ts = next_entry_ts(now, blackout)
    open_ts = open_ts_from_entry(entry_ts, blackout)
    sheet_done = await sheet_already_done(session_date)
    if not should_run(now, sheet_done, open_ts):
        log.info("late pass no-op", extra=kv(sheet_done=sheet_done,
                                             session_date=session_date))
        return {"ran": False, "sheet_done": sheet_done}

    claimed: list = []
    while len(claimed) < batch_max:
        msg = await claim(IN_QUEUE, LATE_CONSUMER)
        if msg is None:
            break
        claimed.append(msg)

    candidates: list[dict] = []
    by_msg: dict[int, object] = {}
    guard_routed = thesis_routed = 0
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
                                 "headline": meta.get("headline"),
                                 "session_date": session_date, "late": True},
                        reason="late-pass overnight item touches an open "
                               "position — routed to A12", conn=conn)
                    await enqueue(GUARD_QUEUE, f"{item_id}:{revision}:a4guard",
                                  msg.payload, priority=0, conn=conn)
            await ack(msg.msg_id)
            guard_routed += 1
            continue

        if not tickers:
            async with pool.connection() as conn:
                async with conn.transaction():
                    await write_decision(
                        signal_id=item_id, item_id=item_id,
                        item_revision=revision,
                        stage="PREMARKET", agent="A4", action="THESIS",
                        payload={"headline": meta.get("headline"),
                                 "session_date": session_date, "late": True},
                        reason="material, no mappable ticker — thesis lane",
                        conn=conn)
                    await enqueue(THESIS_QUEUE, f"{item_id}:{revision}:a4thesis",
                                  msg.payload, priority=100, conn=conn)
            await ack(msg.msg_id)
            thesis_routed += 1
            continue

        candidates.append({"item_id": item_id, "revision": revision,
                           "msg_id": msg.msg_id, "priority": msg.priority,
                           "headline": meta.get("headline"),
                           "tickers": tickers})
        by_msg[msg.msg_id] = msg

    already = await late_forwarded_today(session_date)
    passes_left = passes_remaining(now, open_ts)
    forward, rest, allowance = plan_forwarding(
        candidates, already, late_daily_max, late_pass_max, passes_left)
    budget_after = max(0, late_daily_max - already - len(forward))

    for c in forward:
        msg = by_msg[c["msg_id"]]
        dedup = f"{c['item_id']}:{c['revision']}"
        async with pool.connection() as conn:
            async with conn.transaction():
                await write_decision(
                    signal_id=c["item_id"], item_id=c["item_id"],
                    item_revision=c["revision"], ticker=c["tickers"][0],
                    stage="PREMARKET", agent="A4", action="LATE_CANDIDATE",
                    payload={"headline": c["headline"],
                             "priority": c["priority"],
                             "entry_ts": entry_ts.isoformat(),
                             "pass_allowance": allowance,
                             "passes_left": passes_left,
                             "session_date": session_date},
                    reason="arrived after the 07:00 ET sheet — forwarded to "
                           "the open (late pass)", conn=conn)
                await enqueue_delayed(
                    ANALYST_QUEUE, f"{dedup}:handoff", msg.payload,
                    priority=45, available_ts=entry_ts, conn=conn)
        await ack(msg.msg_id)
        log.info("late candidate", extra=kv(ticker=c["tickers"][0],
                                            item_id=c["item_id"],
                                            priority=c["priority"]))

    deferred = 0
    for c in rest:
        # Lost this pass's ranking, or the daily budget is spent. Either
        # way: refund the attempt and return it to the pool. While budget
        # remains it competes again next pass; once budget is gone (or the
        # open arrives) it simply stays on signal.overnight and tomorrow's
        # 07:00 sheet ranks and journals it like any other overnight item
        # (or bulk-expires it at max_age_hours). No per-item journal row
        # for a scheduling decision — v0.12.16 removed the same-day
        # IGNORE burst that flooded the tape at exhaustion on 2026-08-06.
        await defer(c["msg_id"], DEFER_SECS)
        deferred += 1

    stats = {"ran": True, "session_date": session_date, "fresh": len(claimed),
             "forwarded": len(forward), "deferred": deferred,
             "allowance": allowance, "passes_left": passes_left,
             "budget_left": budget_after,
             "guard_routed": guard_routed, "thesis_routed": thesis_routed,
             "entry_ts": entry_ts.isoformat()}
    log.info("late pass done", extra=kv(**stats))
    return stats


async def main() -> None:
    from common.db import close_pool
    cfg = load_yaml(config_path("a4.yaml"))
    await register_config_version("a4 late pass")
    try:
        await run_late_pass(cfg)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
