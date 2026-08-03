"""Gate veto counterfactuals (v0.12.10) — the measurement half of the §14
gate-threshold design item.

Incident context (2026-08-03, first post-recovery session): the gate went
0-for-21 on placeholder thresholds and there was NO WAY to know what those
vetoes cost or saved — the July 16-17 review had to spot-check tickers by
hand against news. This module makes every FINAL gate veto measurable:

  * record_veto(): one row in journal.gate_counterfactuals per final VETO
    decision, capturing the state at veto time (price, pre-news price,
    pct_move, vol_mult, direction, rule, reason). Called from the veto
    paths in service.py; BEST-EFFORT — a failure here logs a warning and
    never interferes with the veto itself (measurement must not gate).

  * sweep(): after the veto day's session closes, pull the minute bars from
    veto to close ONCE and derive everything retroactively — price 30 min /
    2 h after the veto, the session close, and the maximum favorable /
    adverse excursion from the veto price. Exact bar-derived prices, one
    marketdata call per row, and naturally resilient: a row missed today
    (downtime, API error) is simply filled on a later sweep. Rows that
    still cannot be filled 48 h on are closed out with a note so the
    incomplete set never grows without bound.

Reading the table (a week of data is the §14 input):

    SELECT veto_reason, count(*),
           round(avg(max_up_pct)*100, 2)   AS avg_best_pct,
           round(avg((price_eod-price_at_veto)/price_at_veto)*100, 2)
                                           AS avg_eod_pct
    FROM journal.gate_counterfactuals WHERE complete
    GROUP BY 1 ORDER BY 2 DESC;

A big avg_best_pct on GATE_NO_CONFIRM rows = the gate is leaving money on
the table; near-zero or negative = the vetoes are earning their keep.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from common.clock import utcnow
from common.db import get_pool
from common.log import get_logger, kv

log = get_logger("c3.counterfactual")

FILL_BUFFER_MIN = 20     # wait this long after session close before filling
GIVE_UP_HOURS = 48       # unfillable rows are closed out after this


async def record_veto(*, decision_id: int, signal_id: str,
                      item_id: Optional[str], ticker: str, direction: str,
                      rule: str, veto_reason: str, veto_ts: datetime,
                      price_at_veto: Optional[float],
                      prenews_price: Optional[float],
                      pct_move: Optional[float],
                      vol_mult: Optional[float]) -> None:
    """Insert the veto-time snapshot. Best-effort: never raises."""
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO journal.gate_counterfactuals
                   (decision_id, signal_id, item_id, ticker, direction, rule,
                    veto_reason, veto_ts, price_at_veto, prenews_price,
                    pct_move_at_veto, vol_mult_at_veto)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (decision_id, signal_id, item_id, ticker, direction, rule,
                 veto_reason, veto_ts, price_at_veto, prenews_price,
                 pct_move, vol_mult))
    except Exception as e:                                    # noqa: BLE001
        log.warning("counterfactual record failed",
                    extra=kv(ticker=ticker, decision_id=decision_id,
                             error=repr(e)[:200]))


def derive_outcomes(bars: list[dict], veto_ts: datetime,
                    price_at_veto: Optional[float]) -> dict:
    """Pure: outcome fields from the veto->close minute bars. Checkpoint
    prices use the last bar at or before veto+30m / veto+2h (falling back to
    the first available bar if the tape starts late); a checkpoint past the
    close simply lands on the closing bar. Excursions are signed moves from
    the veto price: max_up_pct is the best case for a long, max_down_pct
    (negative) the best case for the short the LONG_ONLY book didn't take."""
    out = {"price_30m": None, "price_2h": None, "price_eod": None,
           "max_up_pct": None, "max_down_pct": None}
    if not bars:
        return out

    def px_at(delta: timedelta) -> float:
        cutoff = veto_ts + delta
        eligible = [b for b in bars if b["ts"] <= cutoff]
        return (eligible[-1] if eligible else bars[0])["close"]

    out["price_30m"] = px_at(timedelta(minutes=30))
    out["price_2h"] = px_at(timedelta(hours=2))
    out["price_eod"] = bars[-1]["close"]
    if price_at_veto:
        hi = max(b["high"] for b in bars)
        lo = min(b["low"] for b in bars)
        out["max_up_pct"] = round((hi - price_at_veto) / price_at_veto, 5)
        out["max_down_pct"] = round((lo - price_at_veto) / price_at_veto, 5)
    return out


async def sweep(md, now: datetime | None = None, limit: int = 25) -> int:
    """Fill incomplete rows whose session has closed. Returns rows updated."""
    now = now or utcnow()
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT cf_id, ticker, veto_ts, price_at_veto
               FROM journal.gate_counterfactuals
               WHERE NOT complete ORDER BY veto_ts LIMIT %s""", (limit,))
        rows = await cur.fetchall()
    if not rows:
        return 0

    # lazy import avoids a service<->counterfactual import cycle
    from .service import _session_window

    filled = 0
    for cf_id, ticker, veto_ts, price_at_veto in rows:
        stale = (now - veto_ts) > timedelta(hours=GIVE_UP_HOURS)
        session = _session_window(veto_ts)
        close_ts = session[1] if session else None
        if close_ts is None:
            await _finish(cf_id, {}, "no session for veto date")
            filled += 1
            continue
        if now < close_ts + timedelta(minutes=FILL_BUFFER_MIN) and not stale:
            continue                       # session still open — next sweep
        try:
            bars = await md.minute_bars(ticker, veto_ts, close_ts)
        except Exception as e:                                # noqa: BLE001
            log.warning("counterfactual bars fetch failed",
                        extra=kv(ticker=ticker, cf_id=cf_id,
                                 error=repr(e)[:200]))
            bars = []
        if bars:
            price = float(price_at_veto) if price_at_veto is not None else None
            await _finish(cf_id, derive_outcomes(bars, veto_ts, price),
                          f"filled from {len(bars)} bars")
            filled += 1
        elif stale:
            await _finish(cf_id, {}, "gave up: no bars within 48h")
            filled += 1
    return filled


async def _finish(cf_id: int, out: dict, note: str) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """UPDATE journal.gate_counterfactuals
               SET price_30m=%s, price_2h=%s, price_eod=%s,
                   max_up_pct=%s, max_down_pct=%s,
                   complete=true, fill_note=%s
               WHERE cf_id=%s""",
            (out.get("price_30m"), out.get("price_2h"), out.get("price_eod"),
             out.get("max_up_pct"), out.get("max_down_pct"), note[:200],
             cf_id))
