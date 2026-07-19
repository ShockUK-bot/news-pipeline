"""A8 fact sheet — every number computed by SQL/code (baseline rule 5);
the model only narrates. Each section degrades independently to None/empty
so the briefing ALWAYS renders whatever the journal has this morning:

  a4            today's pre-market SHEET decision (stats + ranked entries,
                headlines re-fetched from the news store for citation)
  thesis        active Phase-8 theses + the latest A5 DIGEST stats
  positions     open book with R-progress and the earnings clock per name;
                blackout_soon flags reports within <= blackout_warn sessions
  a6            latest nightly REVIEW (recommendations!) + latest EOD sheet
  earnings      today's market-wide reporter count + held names reporting
  ops           queue depths, non-OK health components, news freshness

Latest-row lookups (not exact-date): on a Monday the newest A6 review is
Friday's — still worth showing, with its run_date attached.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from common.clock import utcnow
from common.db import get_pool
from common.log import get_logger

log = get_logger("a8.facts")

ET = ZoneInfo("America/New_York")


async def _latest_payload(stage: str, agent: str, action: str) -> dict | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT payload FROM journal.decisions
               WHERE stage=%s AND agent=%s AND action=%s
               ORDER BY ts DESC LIMIT 1""", (stage, agent, action))
        row = await cur.fetchone()
        return row[0] if row else None


async def _headlines(item_ids: list[str]) -> dict[str, str]:
    if not item_ids:
        return {}
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT DISTINCT ON (item_id) item_id, headline
               FROM news.news_items WHERE item_id = ANY(%s)
               ORDER BY item_id, revision DESC""", (item_ids,))
        return {r[0]: r[1] for r in await cur.fetchall()}


async def a4_section(session_date: str) -> dict | None:
    payload = await _latest_payload("PREMARKET", "A4", "SHEET")
    if not payload or payload.get("session_date") != session_date:
        return None                       # A4 hasn't run yet this morning
    fwd = payload.get("open_forwarded") or []
    heads = await _headlines([f.get("item_id") for f in fwd
                              if f.get("item_id")])
    for f in fwd:
        f["headline"] = heads.get(f.get("item_id"))
    return {k: payload.get(k) for k in
            ("session_date", "fresh", "open_candidates", "guard_routed",
             "thesis_routed", "ignored", "expired_bulk", "entry_ts",
             "slot")} | {"summary": (payload.get("sheet") or {})
                         .get("summary"), "open_forwarded": fwd}


async def thesis_section() -> dict:
    active: list[dict] = []
    try:
        from a5_thematic.store import load_active
        active = await load_active()
    except Exception as e:
        log.warning("thesis section degraded: %s", repr(e)[:120])
    return {"active": active,
            "digest": await _latest_payload("THEMATIC", "A5", "DIGEST")}


async def positions_section(blackout_warn: int) -> list[dict]:
    try:
        from a6_position_review.context import (load_open_positions,
                                                r_progress)
        from c1_ingestion.earnings import earnings_next_sessions
    except Exception as e:
        log.warning("positions section degraded: %s", repr(e)[:120])
        return []
    out = []
    for p in await load_open_positions():
        last = float(p["last_price"]) if p["last_price"] is not None else None
        ens = await earnings_next_sessions(p["ticker"])
        policy = p["exit_policy"] or {}
        out.append({
            "position_id": p["position_id"], "ticker": p["ticker"],
            "horizon": p["horizon"], "qty_open": p["qty_open"],
            "avg_entry": float(p["avg_entry"]), "last_price": last,
            "r_progress": r_progress(float(p["avg_entry"]),
                                     float(p["r_unit"]), last),
            "current_stop": policy.get("current_stop",
                                       float(p["initial_stop"])),
            "earnings_next_sessions": ens,
            "blackout_soon": ens is not None and ens <= blackout_warn,
        })
    return out


async def a6_section() -> dict:
    return {"review": await _latest_payload("POSITION_REVIEW", "A6",
                                            "REVIEW"),
            "eod": await _latest_payload("POSITION_REVIEW", "A6",
                                         "EOD_SHEET")}


async def earnings_section(held: list[str]) -> dict:
    pool = await get_pool()
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT count(*) FROM news.earnings_calendar
                   WHERE report_date = current_date""")
            total = (await cur.fetchone())[0]
            cur = await conn.execute(
                """SELECT ticker, report_date FROM news.earnings_calendar
                   WHERE ticker = ANY(%s)
                     AND report_date
                         BETWEEN current_date
                             AND current_date + 5
                   ORDER BY report_date""", (held or [""],))
            soon = [{"ticker": r[0], "report_date": r[1].isoformat()}
                    for r in await cur.fetchall()]
        return {"reporting_today": total, "held_reporting_soon": soon}
    except Exception as e:                        # table missing
        log.warning("earnings section degraded: %s", repr(e)[:120])
        return {"reporting_today": None, "held_reporting_soon": []}


async def ops_section() -> dict:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT queue_name, count(*) FROM queue.messages
               WHERE done_ts IS NULL
                 AND queue_name IN ('signal.analyst','signal.overnight',
                                    'signal.thesis','signal.guard')
               GROUP BY queue_name""")
        queues = {r[0]: r[1] for r in await cur.fetchall()}
        cur = await conn.execute(
            """SELECT component, status, detail FROM journal.health
               WHERE status <> 'OK' ORDER BY component""")
        health = [{"component": r[0], "status": r[1],
                   "detail": (r[2] or "")[:120]} for r in await cur.fetchall()]
        cur = await conn.execute("SELECT max(received_ts) FROM news.news_items")
        newest = (await cur.fetchone())[0]
    freshness_h = (round((utcnow() - newest).total_seconds() / 3600, 1)
                   if newest else None)
    return {"queues": queues, "health_not_ok": health,
            "newest_item_age_hours": freshness_h}


async def build_facts(now: datetime | None = None,
                      blackout_warn: int = 2) -> dict:
    now = now or utcnow()
    session_date = now.astimezone(ET).strftime("%Y-%m-%d")
    positions = await positions_section(blackout_warn)
    return {
        "session_date": session_date,
        "a4": await a4_section(session_date),
        "thesis": await thesis_section(),
        "positions": positions,
        "a6": await a6_section(),
        "earnings": await earnings_section([p["ticker"] for p in positions]),
        "ops": await ops_section(),
    }
