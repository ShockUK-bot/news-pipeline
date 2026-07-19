"""A6 context packs — every number computed by SQL/code (baseline rule 5);
the model receives facts and returns judgment, never arithmetic.

One two-join query pulls each open position WITH its originating A2 thesis
payload (the journal-schema-spec §6 "A12 context" pattern). Code adds:
R-progress off C4's mark cache, sessions held (NYSE calendar), today's A12
guard activity, ticker news recency (the evidence-staleness clock for the
long lane), and thesis-store matches from the Phase-8 watchlist.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common.clock import utcnow
from common.db import get_pool
from common.log import get_logger

log = get_logger("a6.context")

ET = ZoneInfo("America/New_York")

_session_count_cache: dict[tuple[str, str], int] = {}


def r_progress(avg_entry: float, r_unit: float,
               last_price: float | None) -> float | None:
    if last_price is None or r_unit <= 0:
        return None
    return round((last_price - avg_entry) / r_unit, 2)


def classify_staleness(days_since_news: float | None, horizon: str,
                       stale_weeks: float, opened_days_ago: float) -> str:
    """Code-side staleness fact (the model gives its own view separately).
    A position younger than the window cannot be stale; None = no news
    since entry, so the clock runs from entry."""
    window_days = stale_weeks * 7
    age = opened_days_ago if days_since_news is None else days_since_news
    if min(age, opened_days_ago) >= window_days:
        return "stale"
    if age >= window_days / 2:
        return "aging"
    return "fresh"


def sessions_held(opened_ts: datetime, now: datetime) -> int:
    import pandas_market_calendars as mcal
    key = (opened_ts.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))
    if key not in _session_count_cache:
        sched = mcal.get_calendar("NYSE").schedule(start_date=key[0],
                                                   end_date=key[1])
        _session_count_cache[key] = max(len(sched) - 1, 0)
    return _session_count_cache[key]


async def load_open_positions(horizon: str | None = None) -> list[dict]:
    """Open positions + originating thesis in one query."""
    sql = """SELECT p.position_id, p.ticker, p.horizon, p.profile,
                    p.opened_ts, p.qty_initial, p.qty_open, p.avg_entry,
                    p.initial_stop, p.r_unit, p.exit_policy, p.last_price,
                    p.realized_pnl, p.thesis_decision_id,
                    d.payload, d.reason, d.confidence
             FROM journal.positions p
             JOIN journal.decisions d ON d.decision_id = p.thesis_decision_id
             WHERE p.status = 'OPEN'"""
    args: tuple = ()
    if horizon is not None:
        sql += " AND p.horizon = %s"
        args = (horizon,)
    sql += " ORDER BY p.position_id"
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args or None)
        rows = await cur.fetchall()
    cols = ("position_id", "ticker", "horizon", "profile", "opened_ts",
            "qty_initial", "qty_open", "avg_entry", "initial_stop", "r_unit",
            "exit_policy", "last_price", "realized_pnl",
            "thesis_decision_id", "thesis_payload", "thesis_reason",
            "thesis_confidence")
    return [dict(zip(cols, r)) for r in rows]


async def guard_activity(position_id: int, since: datetime) -> list[dict]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT g.ts, g.thesis_intact, g.recommended_action,
                      g.action_taken, d.reason
               FROM journal.guard_ledger g
               JOIN journal.decisions d ON d.decision_id = g.decision_id
               WHERE g.position_id = %s AND g.ts >= %s
               ORDER BY g.ts""", (position_id, since))
        return [{"ts": r[0].isoformat(), "thesis_intact": r[1],
                 "recommended_action": r[2], "action_taken": r[3],
                 "reason": (r[4] or "")[:150]}
                for r in await cur.fetchall()]


async def ticker_news_recency(ticker: str, since: datetime) -> dict:
    """Escalated-news clock for the staleness rule: how much material news
    has the pipeline seen on this name, and when was the last item?"""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT count(*), max(ts) FROM journal.decisions
               WHERE ticker = %s AND stage = 'TRIAGE'
                 AND action = 'ESCALATE' AND ts >= %s""", (ticker, since))
        n, last_ts = await cur.fetchone()
    return {"escalated_since_entry": int(n or 0),
            "last_escalation_ts": last_ts.isoformat() if last_ts else None,
            "days_since_news": (round((utcnow() - last_ts).total_seconds()
                                      / 86400, 1) if last_ts else None)}


async def thesis_store_matches(ticker: str) -> list[dict]:
    pool = await get_pool()
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT thesis_id, title, direction, confidence
                   FROM journal.thesis_watchlist WHERE ticker = %s""",
                (ticker,))
            return [{"thesis_id": r[0], "title": r[1], "direction": r[2],
                     "confidence": r[3]} for r in await cur.fetchall()]
    except Exception:                                    # store not migrated
        return []


async def build_pack(pos: dict, now: datetime, stale_weeks: float) -> dict:
    """The per-position fact pack for the nightly review prompt."""
    opened: datetime = pos["opened_ts"]
    news = await ticker_news_recency(pos["ticker"], opened)
    opened_days = round((now - opened).total_seconds() / 86400, 1)
    policy = pos["exit_policy"] or {}
    rp = r_progress(float(pos["avg_entry"]), float(pos["r_unit"]),
                    float(pos["last_price"]) if pos["last_price"] is not None
                    else None)
    return {
        "position_id": pos["position_id"], "ticker": pos["ticker"],
        "horizon": pos["horizon"], "profile": pos["profile"],
        "opened_days_ago": opened_days,
        "sessions_held": sessions_held(opened, now),
        "qty_open": pos["qty_open"], "qty_initial": pos["qty_initial"],
        "avg_entry": float(pos["avg_entry"]),
        "last_price": (float(pos["last_price"])
                       if pos["last_price"] is not None else None),
        "r_progress": rp,
        "current_stop": policy.get("current_stop",
                                   float(pos["initial_stop"])),
        "realized_pnl": float(pos["realized_pnl"]),
        "thesis": (pos["thesis_payload"] or {}).get("thesis")
                  or pos["thesis_payload"],
        "thesis_reason": (pos["thesis_reason"] or "")[:300],
        "news_recency": news,
        "staleness_code": classify_staleness(
            news["days_since_news"], pos["horizon"], stale_weeks,
            opened_days),
        "guard_today": await guard_activity(
            pos["position_id"], now - timedelta(hours=24)),
        "thesis_store_matches": await thesis_store_matches(pos["ticker"]),
    }
