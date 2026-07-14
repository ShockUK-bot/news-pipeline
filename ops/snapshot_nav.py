"""Nightly portfolio NAV snapshot for the C6 Performance tab.
Writes one row/day to journal.portfolio_nav_daily. Baseline capital is
captured once -- the trading_capital value in effect the day of the first
trade -- and frozen from then on (journal.control key
'performance_baseline_capital'), so later CAPITAL top-ups don't reshape the
historical curve. Run after the session-close pass, before A11.

Run: PYTHONPATH=src .venv/bin/python ops/snapshot_nav.py
Env: PIPELINE_DSN
"""
from __future__ import annotations
import asyncio
import os
import psycopg
from psycopg.rows import dict_row


async def snapshot(dsn: str) -> None:
    async with await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row) as conn:
        first = await (await conn.execute(
            "SELECT MIN(opened_ts)::date AS d FROM journal.positions")).fetchone()
        if not first or not first["d"]:
            return  # no trades yet
        baseline = await (await conn.execute(
            "SELECT value FROM journal.control "
            "WHERE key='performance_baseline_capital'")).fetchone()
        if not baseline:
            cap = await (await conn.execute(
                "SELECT value FROM journal.control "
                "WHERE key='trading_capital'")).fetchone()
            await conn.execute(
                "INSERT INTO journal.control (key, value, updated_ts) "
                "VALUES ('performance_baseline_capital', %s, now())", (cap["value"],))
        realized = await (await conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) AS v FROM journal.exits "
            "WHERE ts::date <= current_date")).fetchone()
        unrealized = await (await conn.execute(
            "SELECT COALESCE(SUM((COALESCE(last_price, avg_entry) - avg_entry) "
            "* qty_open),0) AS v FROM journal.positions "
            "WHERE status='OPEN'")).fetchone()
        total = float(realized["v"]) + float(unrealized["v"])
        await conn.execute(
            "INSERT INTO journal.portfolio_nav_daily "
            "(nav_date, realized_pnl_cum, unrealized_pnl, total_pnl) "
            "VALUES (current_date, %s, %s, %s) "
            "ON CONFLICT (nav_date) DO UPDATE SET "
            "realized_pnl_cum=EXCLUDED.realized_pnl_cum, "
            "unrealized_pnl=EXCLUDED.unrealized_pnl, total_pnl=EXCLUDED.total_pnl",
            (realized["v"], unrealized["v"], total))
        await conn.commit()


if __name__ == "__main__":
    asyncio.run(snapshot(os.environ["PIPELINE_DSN"]))
