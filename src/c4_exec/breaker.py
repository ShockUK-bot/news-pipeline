"""Drawdown breaker (phase4-design-v1_0 D7-confirmed: -2% daily).

Day PnL = realized (exits journaled today, UTC session date) + unrealized
(open positions marked at last_price). Trip when day PnL <= -2% of effective
capital: set drawdown_breaker=1 (audited, actor C4). ONE-WAY — code never
resets it; the operator does, from the dashboard, deliberately (runbook §5).
A3 vetoes and C4 pre-flight both already honor the flag; exits continue.
"""
from __future__ import annotations

from common.db import get_pool
from common.log import get_logger, kv

from .flags import breaker_on, get_flag, set_flag

log = get_logger("c4.breaker")


async def day_pnl() -> dict:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT COALESCE(sum(realized_pnl),0) FROM journal.exits
               WHERE ts::date = (now() AT TIME ZONE 'UTC')::date""")
        realized = float((await cur.fetchone())[0])
        cur = await conn.execute(
            """SELECT COALESCE(sum((last_price - avg_entry) * qty_open),0)
               FROM journal.positions
               WHERE status='OPEN' AND last_price IS NOT NULL""")
        unrealized = float((await cur.fetchone())[0])
    return {"realized": realized, "unrealized": unrealized,
            "total": realized + unrealized}


async def check_breaker(drawdown_pct: float) -> bool:
    """Returns True if the breaker is (now) tripped."""
    if await breaker_on():
        return True
    equity = float(await get_flag("broker_equity", "0") or 0)
    capital = float(await get_flag("trading_capital", "0") or 0)
    effective = min(equity, capital)
    if effective <= 0:
        return False
    pnl = await day_pnl()
    threshold = -drawdown_pct * effective
    if pnl["total"] <= threshold:
        await set_flag("drawdown_breaker", "1", "C4",
                       f"BREAKER_TRIP day_pnl={pnl['total']:.2f} "
                       f"(realized={pnl['realized']:.2f} "
                       f"unrealized={pnl['unrealized']:.2f}) "
                       f"threshold={threshold:.2f}")
        log.warning("drawdown breaker TRIPPED",
                    extra=kv(day_pnl=round(pnl["total"], 2),
                             threshold=round(threshold, 2)))
        return True
    return False

