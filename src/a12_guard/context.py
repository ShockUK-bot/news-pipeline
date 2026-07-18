"""A12 context pack — code-computed facts the guard sees beyond the item.

Deliberately smaller than A2's pack: the guard needs the position, the
original thesis, and price action since the news. It does NOT get related
headlines or regime features — the question is "is THIS thesis broken by
THIS item", not "what does the world look like".

All numbers are computed here (baseline rule 5); the model never derives
R-progress or percentages itself.
"""
from __future__ import annotations

from datetime import timedelta

from common.clock import parse_ts, utcnow
from common.db import get_pool
from common.log import get_logger
from common.marketdata import MarketData

log = get_logger("a12.context")


async def fetch_thesis(thesis_decision_id: int) -> dict:
    """The A2 thesis that justified the entry (positions.thesis_decision_id
    is NOT NULL by design — NO_THESIS_LINEAGE is vetoed at A3)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT payload, reason FROM journal.decisions WHERE decision_id = %s",
            (thesis_decision_id,))
        row = await cur.fetchone()
    if row is None:
        return {}
    payload, reason = row[0] or {}, row[1]
    thesis = payload.get("thesis") or {}
    return {
        "direction": thesis.get("direction"),
        "magnitude_est": thesis.get("magnitude_est"),
        "expected_move_window": thesis.get("expected_move_window"),
        "horizon": thesis.get("horizon"),
        "priced_in_assessment": thesis.get("priced_in_assessment"),
        "source_risk": thesis.get("source_risk"),
        "reason": thesis.get("reason") or reason,
    }


def position_pack(pos: dict) -> dict:
    """The position as the guard sees it. Watch-list comes from the LIVE
    exit_policy state on the position row (A2 authored it; A3 attached it;
    C4 carries it)."""
    policy = pos.get("exit_policy") or {}
    opened = pos.get("opened_ts")
    age_sessions = None
    if opened is not None:
        age_sessions = round((utcnow() - opened).total_seconds() / 86400.0, 1)
    return {
        "ticker": pos["ticker"],
        "horizon": pos.get("horizon"),
        "profile": pos.get("profile"),
        "qty_open": int(pos["qty_open"]),
        "avg_entry": float(pos["avg_entry"]),
        "current_stop": policy.get("current_stop") or float(pos["initial_stop"]),
        "opened_days_ago": age_sessions,
        "scale_out_done": bool(policy.get("scale_out_done", False)),
        "watch_list": list(policy.get("news_invalidations") or []),
    }


async def build_guard_context(md: MarketData, item: dict, pos: dict) -> dict:
    """Price action since the news, plus unrealized R. Degrades to nulls when
    market data is unavailable — the guard still runs (an invalidating item
    with no quote is still an invalidating item); missing numbers are visible
    in the journal payload."""
    received = parse_ts(item.get("received_ts") or item["published_ts"])
    now = utcnow()
    last = None
    try:
        quote = await md.snapshot(pos["ticker"])
        last = quote.price
    except Exception as e:                          # fail-visible, not fail-crash
        log.warning("guard snapshot unavailable for %s: %r", pos["ticker"], e)

    pre_bars = []
    try:
        pre_bars = await md.minute_bars(pos["ticker"],
                                        received - timedelta(minutes=30), received)
    except Exception:
        pass
    prenews = pre_bars[-1]["close"] if pre_bars else None

    avg_entry = float(pos["avg_entry"])
    r_unit = float(pos["r_unit"])
    unrealized_r = round((last - avg_entry) / r_unit, 2) if last else None
    pct_since_news = (round((last - prenews) / prenews, 5)
                      if (last and prenews) else None)

    return {
        "price_action": {
            "last": last,
            "prenews_price": prenews,
            "pct_move_since_news": pct_since_news,
            "minutes_since_news": int((now - received).total_seconds() // 60),
            "unrealized_r": unrealized_r,
        },
    }
