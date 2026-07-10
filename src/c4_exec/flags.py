"""Operational controls + audit (phase4-design-v1_0 D2/D4 + C6 contract).

journal.control is the single flag store: the dashboard flips values, ONLY
code enforces them. C4 checks kill_switch before every submission; A3 reads
the same rows at sizing. Every mutation writes an audit row.

Keys: kill_switch, drawdown_breaker, block_entries ('0'/'1'),
trading_capital, max_trades_per_day (numbers as text),
broker_equity, settled_cash, last_reconcile_ts (C4-written, read-only to UI).
"""
from __future__ import annotations

from common.db import get_pool
from common.log import get_logger, kv

log = get_logger("c4.flags")

DEFAULTS = {"kill_switch": "0", "drawdown_breaker": "0", "block_entries": "0",
            "max_trades_per_day": "5"}


async def ensure_defaults() -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        for k, v in DEFAULTS.items():
            await conn.execute(
                """INSERT INTO journal.control (key, value, updated_ts)
                   VALUES (%s, %s, now()) ON CONFLICT (key) DO NOTHING""", (k, v))


async def get_flag(key: str, default: str = "0") -> str:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT value FROM journal.control WHERE key=%s", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_flag(key: str, value: str, actor: str, detail: str = "",
                   conn=None) -> None:
    async def _run(c):
        cur = await c.execute(
            "SELECT value FROM journal.control WHERE key=%s", (key,))
        row = await cur.fetchone()
        old = row[0] if row else None
        await c.execute(
            """INSERT INTO journal.control (key, value, updated_ts)
               VALUES (%s,%s,now())
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                                               updated_ts=now()""", (key, value))
        await c.execute(
            """INSERT INTO journal.audit (actor, action, old_value, new_value, detail)
               VALUES (%s,%s,%s,%s,%s)""",
            (actor, f"{key.upper()}_SET", old, value, detail[:300]))
    if conn is not None:
        await _run(conn)
    else:
        pool = await get_pool()
        async with pool.connection() as c:
            await _run(c)
    log.info("control set", extra=kv(key=key, value=value, actor=actor))


async def kill_switch_on() -> bool:
    return await get_flag("kill_switch") == "1"


async def breaker_on() -> bool:
    return await get_flag("drawdown_breaker") == "1"


async def entries_blocked() -> bool:
    """Any of the three entry-blocking flags (the D4 ladder's BLOCK_ENTRIES
    plus kill and breaker) — C4's single pre-submission check."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT key, value FROM journal.control
               WHERE key IN ('kill_switch','drawdown_breaker','block_entries')""")
        rows = await cur.fetchall()
    return any(v == "1" for _, v in rows)

