"""C4 persistence — the order state machine's DB operations. Intents table is
the authority; orders/fills/positions/position_events/exits record every
transition (journal schema v1, populated from Phase 4 on).

State maps: broker status -> orders.state
  accepted -> ACCEPTED, partially_filled -> PARTIAL, filled -> FILLED,
  canceled -> CANCELLED, rejected -> REJECTED, expired -> EXPIRED.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from common.broker import BrokerOrder
from common.db import get_pool, jb
from common.log import get_logger, kv

log = get_logger("c4.state")

BROKER_STATE = {"accepted": "ACCEPTED", "new": "ACCEPTED",
                "partially_filled": "PARTIAL", "filled": "FILLED",
                "canceled": "CANCELLED", "rejected": "REJECTED",
                "expired": "EXPIRED"}


def order_state(o: BrokerOrder) -> str:
    return BROKER_STATE.get(o.status, "ACCEPTED")


async def create_order(intent_id: Optional[str], role: str, o: BrokerOrder,
                       position_id: Optional[int] = None, conn=None) -> int:
    async def _run(c):
        cur = await c.execute(
            """INSERT INTO journal.orders
               (intent_id, position_id, broker_order_id, order_role, state,
                qty, limit_price, stop_price, submitted_ts, raw)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING order_id""",
            (intent_id, position_id, o.broker_order_id, role, order_state(o),
             o.qty, o.limit_price, o.stop_price, o.submitted_ts, jb(o.raw)))
        return (await cur.fetchone())[0]
    if conn is not None:
        return await _run(conn)
    pool = await get_pool()
    async with pool.connection() as c:
        return await _run(c)


async def transition_order(order_id: int, o: BrokerOrder, conn=None) -> None:
    async def _run(c):
        state = order_state(o)
        await c.execute(
            """UPDATE journal.orders SET state=%s, raw=%s,
                      closed_ts = CASE WHEN %s IN
                        ('FILLED','CANCELLED','REJECTED','EXPIRED')
                        THEN now() ELSE closed_ts END
               WHERE order_id=%s""",
            (state, jb(o.raw), state, order_id))
    if conn is not None:
        await _run(conn)
    else:
        pool = await get_pool()
        async with pool.connection() as c:
            await _run(c)


async def record_fill(order_id: int, ts: datetime, qty: int, price: float,
                      broker_exec_id: str, conn=None) -> None:
    async def _run(c):
        await c.execute(
            """INSERT INTO journal.fills (order_id, ts, qty, price, broker_exec_id)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (broker_exec_id) DO NOTHING""",
            (order_id, ts, qty, price, broker_exec_id))
    if conn is not None:
        await _run(conn)
    else:
        pool = await get_pool()
        async with pool.connection() as c:
            await _run(c)


async def create_position(ticker: str, horizon: str, profile: str,
                          entry_intent_id: str, thesis_decision_id: int,
                          item_id: Optional[str], qty: int, avg_entry: float,
                          initial_stop: float, exit_policy: dict,
                          config_version: str, opened_ts: datetime,
                          origin: str = "news", conn=None) -> int:
    r_unit = round(avg_entry - initial_stop, 4)
    async def _run(c):
        cur = await c.execute(
            """INSERT INTO journal.positions
               (ticker, horizon, profile, status, opened_ts, entry_intent_id,
                thesis_decision_id, item_id, qty_initial, qty_open, avg_entry,
                initial_stop, r_unit, exit_policy, config_version, origin)
               VALUES (%s,%s,%s,'OPEN',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING position_id""",
            (ticker, horizon, profile, opened_ts, entry_intent_id,
             thesis_decision_id, item_id, qty, qty, avg_entry, initial_stop,
             r_unit, jb(exit_policy), config_version, origin))
        return (await cur.fetchone())[0]
    if conn is not None:
        return await _run(conn)
    pool = await get_pool()
    async with pool.connection() as c:
        return await _run(c)


async def position_event(position_id: int, event_type: str, actor: str,
                         old_value=None, new_value=None,
                         r_progress: Optional[float] = None,
                         detail: str = "", decision_id: Optional[int] = None,
                         conn=None) -> None:
    async def _run(c):
        await c.execute(
            """INSERT INTO journal.position_events
               (position_id, event_type, actor, old_value, new_value,
                r_progress, detail, decision_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (position_id, event_type, actor,
             jb(old_value) if old_value is not None else None,
             jb(new_value) if new_value is not None else None,
             r_progress, detail[:300], decision_id))
    if conn is not None:
        await _run(conn)
    else:
        pool = await get_pool()
        async with pool.connection() as c:
            await _run(c)


async def record_exit(position_id: int, order_id: Optional[int], ts: datetime,
                      exit_layer: str, qty: int, price: float,
                      avg_entry: float, r_unit: float, is_partial: bool,
                      conn=None) -> None:
    pnl = round((price - avg_entry) * qty, 4)
    r_multiple = round(pnl / (r_unit * qty), 3) if r_unit else 0.0
    async def _run(c):
        await c.execute(
            """INSERT INTO journal.exits
               (position_id, order_id, ts, exit_layer, qty, price,
                realized_pnl, r_multiple, is_partial)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (position_id, order_id, ts, exit_layer, qty, price, pnl,
             r_multiple, is_partial))
        await c.execute(
            """UPDATE journal.positions
               SET qty_open = qty_open - %s,
                   realized_pnl = realized_pnl + %s,
                   status = CASE WHEN qty_open - %s <= 0 THEN 'CLOSED'
                                 ELSE status END,
                   closed_ts = CASE WHEN qty_open - %s <= 0 THEN now()
                                    ELSE closed_ts END
               WHERE position_id=%s""",
            (qty, pnl, qty, qty, position_id))
        await position_event(position_id, "EXIT" if not is_partial else "SCALE_OUT",
                             "C4", new_value={"layer": exit_layer, "qty": qty,
                                              "price": price, "pnl": pnl},
                             detail=exit_layer, conn=c)
    if conn is not None:
        await _run(conn)
    else:
        pool = await get_pool()
        async with pool.connection() as c:
            await _run(c)


async def open_positions() -> list[dict]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT position_id, ticker, horizon, profile, qty_open,
                      avg_entry, initial_stop, r_unit, exit_policy,
                      catastrophe_stop_order_id, opened_ts, realized_pnl,
                      last_price
               FROM journal.positions WHERE status='OPEN'""")
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in await cur.fetchall()]

