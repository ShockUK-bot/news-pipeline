"""C4 exit mechanics (phase4-design-v1_0 D3).

Every synthetic-layer exit follows the same sequence — the position is never
knowingly unprotected for more than exit_unprotected_max_secs:

  1. cancel the broker-resident catastrophe stop (journal the cancel)
  2. submit the exit as a marketable limit at the bid
  3. poll: filled -> exits row (layer attribution) + events; if SCALE_OUT,
     re-place the catastrophe for the remaining shares
  4. NOT filled within the window -> cancel the exit, REINSTATE the
     catastrophe stop, journal EXIT_REINSTATED — the exit attempt failed but
     the position is protected again; the next bar re-evaluates.

Catastrophe fills found at the broker (tier-1 fired on its own) are recorded
by record_catastrophe_fill during the poll loop / reconciliation.
"""
from __future__ import annotations

import uuid
from typing import Optional

from common.broker import Broker, BrokerReject
from common.clock import utcnow
from common.db import get_pool
from common.log import get_logger, kv

from .state import (create_order, position_event, record_exit,
                    transition_order)

log = get_logger("c4.mechanics")


async def _current_catastrophe(position_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT o.order_id, o.broker_order_id, o.qty, o.stop_price
               FROM journal.orders o
               JOIN journal.positions p
                 ON p.catastrophe_stop_order_id = o.order_id
               WHERE p.position_id=%s""", (position_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        return {"order_id": row[0], "broker_order_id": row[1],
                "qty": row[2], "stop_price": float(row[3])}


async def _place_catastrophe(broker: Broker, pos: dict, qty: int,
                             stop_price: float) -> int:
    o = await broker.submit_stop(pos["ticker"], "SELL", qty, stop_price,
                                 client_order_id=f"cat-{uuid.uuid4().hex[:12]}")
    order_row = await create_order(None, "CATASTROPHE_STOP", o,
                                   position_id=pos["position_id"])
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """UPDATE journal.positions SET catastrophe_stop_order_id=%s
               WHERE position_id=%s""", (order_row, pos["position_id"]))
    return order_row


async def execute_exit(broker: Broker, pos: dict, qty: int, layer: str,
                       reason: str, bid: float, now_fn,
                       unprotected_max_secs: float = 45.0,
                       poll_sleep: float = 1.0,
                       sleep_fn=None) -> str:
    """Returns 'FILLED' | 'REINSTATED' | 'CATASTROPHE_FILLED'."""
    import asyncio
    sleep = sleep_fn or asyncio.sleep
    position_id = pos["position_id"]
    is_partial = qty < int(pos["qty_open"])

    cat = await _current_catastrophe(position_id)
    if cat is not None:
        cancelled = await broker.cancel(cat["broker_order_id"])
        if not cancelled:
            # cancel failed because the stop is terminal OR unknown: the
            # catastrophe may have FILLED broker-side — check, but a broker
            # that can't even find the order must not crash the engine
            try:
                co = await broker.get_order(cat["broker_order_id"])
            except Exception:
                co = None
                await position_event(position_id, "GUARD_ACTION", "C4",
                                     detail="catastrophe order unknown at "
                                            "broker during exit — proceeding "
                                            "with exit, protection state "
                                            "uncertain")
            if co is not None and co.status == "filled":
                await transition_order(cat["order_id"], co)
                await record_exit(position_id, cat["order_id"], now_fn(),
                                  "CATASTROPHE", co.filled_qty,
                                  float(co.filled_avg_price),
                                  float(pos["avg_entry"]),
                                  float(pos["r_unit"]), is_partial=False)
                log.warning("catastrophe had already filled",
                            extra=kv(position_id=position_id))
                return "CATASTROPHE_FILLED"

    try:
        exit_order = await broker.submit_limit(
            pos["ticker"], "SELL", qty, round(bid, 2),
            client_order_id=f"exit-{uuid.uuid4().hex[:12]}", tif="day")
    except BrokerReject as e:
        # protect first, diagnose second
        if cat is not None:
            await _place_catastrophe(broker, pos, int(pos["qty_open"]),
                                     cat["stop_price"])
        await position_event(position_id, "GUARD_ACTION", "C4",
                             detail=f"exit submit rejected: {e}", )
        return "REINSTATED"

    order_row = await create_order(None, "EXIT" if not is_partial
                                   else "SCALE_OUT", exit_order,
                                   position_id=position_id)
    waited = 0.0
    while waited < unprotected_max_secs:
        o = await broker.get_order(exit_order.broker_order_id)
        if o.status == "filled":
            await transition_order(order_row, o)
            await record_exit(position_id, order_row, now_fn(), layer,
                              o.filled_qty, float(o.filled_avg_price),
                              float(pos["avg_entry"]), float(pos["r_unit"]),
                              is_partial=is_partial)
            remaining = int(pos["qty_open"]) - o.filled_qty
            if remaining > 0 and cat is not None:
                await _place_catastrophe(broker, pos, remaining,
                                         cat["stop_price"])
            log.info("exit filled", extra=kv(position_id=position_id,
                                             layer=layer, qty=o.filled_qty,
                                             price=o.filled_avg_price))
            return "FILLED"
        await sleep(poll_sleep)
        waited += poll_sleep

    # window expired: cancel exit, reinstate protection
    await broker.cancel(exit_order.broker_order_id)
    final = await broker.get_order(exit_order.broker_order_id)
    await transition_order(order_row, final)
    if final.status == "filled":                     # raced the cancel
        await record_exit(position_id, order_row, now_fn(), layer,
                          final.filled_qty, float(final.filled_avg_price),
                          float(pos["avg_entry"]), float(pos["r_unit"]),
                          is_partial=is_partial)
        return "FILLED"
    if cat is not None:
        await _place_catastrophe(broker, pos, int(pos["qty_open"]),
                                 cat["stop_price"])
    await position_event(position_id, "GUARD_ACTION", "C4",
                         detail=f"EXIT_REINSTATED: {layer} exit unfilled in "
                                f"{unprotected_max_secs}s, catastrophe re-placed",
                         new_value={"layer": layer, "qty": qty})
    log.warning("exit reinstated", extra=kv(position_id=position_id,
                                            layer=layer))
    return "REINSTATED"

