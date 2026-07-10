"""C4 Execution service — Phase 4 chunk 1: reconciliation gate + entry flow.
(Exit engine, overnight rule, and dead-man monitors land in chunk 2; the
module boundaries here are built for them.)

Entry flow per intent on exec.intent:
  1. idempotency: intent already SUBMITTED/FILLED or an ENTRY order exists
     for it -> no-op ack (crash-replay safe at BOTH layers: intent_id here,
     client_order_id at the broker).
  2. pre-flight (deployed notional + new <= effective capital; settled cash;
     kill/breaker/BLOCK_ENTRIES; halt) — a second, independent enforcement
     of what A3 already checked, because A3's read and C4's submit are
     different moments.
  3. submit limit DAY, client_order_id = intent_id.
  4. poll to terminal (entry orders are DAY-limited; polling cadence 1s in
     tests via injectable sleep, 2s live).
  5. on fill: ONE TRANSACTION — position row + STOPS_PLACED event + EXEC
     decision; then place the broker-resident catastrophe stop (stop-market
     GTC at the policy's materialized price, never moved) and link it.
Partial fills at day end: position sized to filled qty, remainder expires.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal as _signal
from typing import Optional

from common.broker import BrokerReject, get_broker
from common.clock import utcnow
from common.config import config_path, load_yaml
from common.db import get_pool, close_pool
from common.journal import (active_config_version, register_config_version,
                            write_decision)
from common.log import get_logger, kv
from common.queue import ack, claim, enqueue, fail, wait_for_message
from c1_ingestion.heartbeat import set_health

from .flags import ensure_defaults, entries_blocked, get_flag
from .reconcile import reconcile
from .state import (create_order, create_position, open_positions,
                    position_event, record_fill, transition_order)

log = get_logger("c4.service")

IN_QUEUE = "exec.intent"
CONSUMER = f"c4-{os.getpid()}"


class C4Service:
    def __init__(self, cfg: dict, broker=None, now_fn=None,
                 poll_sleep: float = 2.0, fill_timeout_secs: float = 300.0):
        self.cfg = cfg["c4"]
        self.broker = broker or get_broker()
        self.now_fn = now_fn or utcnow
        self.poll_sleep = poll_sleep
        self.fill_timeout_secs = fill_timeout_secs

    # ---------------------------------------------------------------- entries
    async def _already_processed(self, intent_id: str) -> bool:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT status FROM journal.intents WHERE intent_id=%s""",
                (intent_id,))
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"intent not in journal: {intent_id}")
            if row[0] in ("SUBMITTED", "FILLED", "PARTIAL", "CANCELLED",
                          "EXPIRED", "REJECTED"):
                return True
            cur = await conn.execute(
                """SELECT count(*) FROM journal.orders
                   WHERE intent_id=%s AND order_role='ENTRY'""", (intent_id,))
            return (await cur.fetchone())[0] > 0

    async def _preflight(self, body: dict) -> Optional[str]:
        if await entries_blocked():
            return "ENTRIES_BLOCKED"
        equity = float(await get_flag("broker_equity", "0") or 0)
        capital = float(await get_flag("trading_capital", "0") or 0)
        effective = min(equity, capital)
        settled = float(await get_flag("settled_cash", "0") or 0)
        notional = body["qty"] * body["limit_price"]
        deployed = sum(p["qty_open"] * float(p["avg_entry"])
                       for p in await open_positions())
        if deployed + notional > effective:
            return "CAPITAL_PREFLIGHT"
        if notional > settled:
            return "SETTLED_CASH"
        return None

    async def _set_intent_status(self, intent_id: str, status: str,
                                 conn=None) -> None:
        async def _run(c):
            await c.execute(
                "UPDATE journal.intents SET status=%s WHERE intent_id=%s",
                (status, intent_id))
        if conn is not None:
            await _run(conn)
        else:
            pool = await get_pool()
            async with pool.connection() as c:
                await _run(c)

    async def handle_intent(self, msg) -> None:
        body = msg.payload.get("body") or {}
        trace = msg.payload.get("envelope", {}).get("trace", {})
        intent_id = body.get("intent_id")
        if not intent_id or not body.get("ticker") or not body.get("qty"):
            raise ValueError(f"malformed intent message ({msg.dedup_key})")

        if await self._already_processed(intent_id):
            log.info("intent no-op (idempotent)", extra=kv(intent_id=intent_id))
            return

        veto = await self._preflight(body)
        if veto:
            await self._set_intent_status(intent_id, "REJECTED")
            await write_decision(
                signal_id=trace.get("signal_id") or intent_id,
                item_id=trace.get("item_id"), ticker=body["ticker"],
                stage="ORDER", agent="C4", action="VETO", veto_reason=veto,
                payload={"intent_id": intent_id, "qty": body["qty"],
                         "limit_price": body["limit_price"]},
                reason=f"pre-flight {veto}")
            log.warning("pre-flight veto", extra=kv(intent_id=intent_id,
                                                    reason=veto))
            return

        try:
            border = await self.broker.submit_limit(
                body["ticker"], "BUY", int(body["qty"]),
                float(body["limit_price"]), client_order_id=intent_id,
                tif="day")
        except BrokerReject as e:
            await self._set_intent_status(intent_id, "REJECTED")
            await write_decision(
                signal_id=trace.get("signal_id") or intent_id,
                item_id=trace.get("item_id"), ticker=body["ticker"],
                stage="ORDER", agent="C4", action="VETO",
                veto_reason="BROKER_REJECT",
                payload={"intent_id": intent_id, "error": str(e)[:300]},
                reason="broker rejected entry")
            return

        order_id = await create_order(intent_id, "ENTRY", border)
        await self._set_intent_status(intent_id, "SUBMITTED")
        log.info("entry submitted", extra=kv(intent_id=intent_id,
                                             broker_order_id=border.broker_order_id))

        border = await self._await_terminal(border.broker_order_id)
        await transition_order(order_id, border)

        if border.filled_qty <= 0:
            await self._set_intent_status(
                intent_id, "EXPIRED" if border.status == "expired" else "CANCELLED")
            log.info("entry unfilled", extra=kv(intent_id=intent_id,
                                                status=border.status))
            return

        fill_price = float(border.filled_avg_price)
        await record_fill(order_id, self.now_fn(), border.filled_qty,
                          fill_price, f"{border.broker_order_id}-fill")
        await self._set_intent_status(
            intent_id, "FILLED" if border.filled_qty >= border.qty else "PARTIAL")
        await self._open_position(body, trace, intent_id, order_id,
                                  border.filled_qty, fill_price)

    async def _await_terminal(self, broker_order_id: str):
        waited = 0.0
        while True:
            o = await self.broker.get_order(broker_order_id)
            if o.terminal:
                return o
            if waited >= self.fill_timeout_secs:
                await self.broker.cancel(broker_order_id)
                return await self.broker.get_order(broker_order_id)
            await asyncio.sleep(self.poll_sleep)
            waited += self.poll_sleep

    async def _open_position(self, body: dict, trace: dict, intent_id: str,
                             entry_order_id: int, qty: int,
                             fill_price: float) -> None:
        policy = dict(body["exit_policy"])
        atr = float(policy["atr_14"])
        # re-materialize stops off the ACTUAL fill (A3 anticipated the limit)
        k = float(policy["initial_stop"]["k"])
        cat_k = float(policy["catastrophe_stop_broker"]["k"])
        policy["initial_stop"]["price"] = round(fill_price - k * atr, 2)
        policy["catastrophe_stop_broker"]["price"] = round(
            fill_price - cat_k * atr, 2)

        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                position_id = await create_position(
                    ticker=body["ticker"], horizon=body["horizon"],
                    profile=policy["profile"], entry_intent_id=intent_id,
                    thesis_decision_id=body["thesis_decision_id"],
                    item_id=trace.get("item_id"), qty=qty,
                    avg_entry=fill_price,
                    initial_stop=policy["initial_stop"]["price"],
                    exit_policy=policy,
                    config_version=active_config_version(),
                    opened_ts=self.now_fn(), conn=conn)
                await conn.execute(
                    """UPDATE journal.orders SET position_id=%s
                       WHERE order_id=%s""", (position_id, entry_order_id))
                await write_decision(
                    signal_id=trace.get("signal_id") or intent_id,
                    item_id=trace.get("item_id"), ticker=body["ticker"],
                    stage="ORDER", agent="C4", action="FILLED",
                    payload={"intent_id": intent_id, "position_id": position_id,
                             "qty": qty, "fill_price": fill_price,
                             "exit_policy": policy},
                    reason=f"entry filled {qty} @ {fill_price}", conn=conn)

        # catastrophe stop OUTSIDE the tx: a fill without its stop must be
        # retried, not rolled back (the position exists at the broker either way)
        cat_price = policy["catastrophe_stop_broker"]["price"]
        try:
            stop_order = await self.broker.submit_stop(
                body["ticker"], "SELL", qty, cat_price,
                client_order_id=f"cat-{intent_id}")
            stop_row_id = await create_order(None, "CATASTROPHE_STOP",
                                             stop_order,
                                             position_id=position_id)
            async with pool.connection() as conn:
                await conn.execute(
                    """UPDATE journal.positions
                       SET catastrophe_stop_order_id=%s WHERE position_id=%s""",
                    (stop_row_id, position_id))
            await position_event(position_id, "STOPS_PLACED", "C4",
                                 new_value={"initial_stop": policy["initial_stop"],
                                            "catastrophe": {
                                                "price": cat_price,
                                                "broker_order_id":
                                                    stop_order.broker_order_id}},
                                 detail="two-tier stops armed")
        except Exception as e:
            # position exists but is protected only by synthetic layers: alarm
            await set_health("exec", "DEGRADED",
                             f"CATASTROPHE STOP FAILED {body['ticker']}: {repr(e)[:150]}")
            await position_event(position_id, "STOPS_PLACED", "C4",
                                 new_value={"initial_stop": policy["initial_stop"],
                                            "catastrophe": None},
                                 detail=f"CATASTROPHE FAILED: {repr(e)[:150]}")
            log.error("catastrophe stop placement failed",
                      extra=kv(position_id=position_id, error=repr(e)[:200]))
            return
        log.info("position opened", extra=kv(position_id=position_id,
                                             ticker=body["ticker"], qty=qty,
                                             fill=fill_price, cat=cat_price))


async def consume_loop(svc: C4Service, stop: asyncio.Event) -> None:
    await set_health("exec", "OK", f"consuming {IN_QUEUE}")
    while not stop.is_set():
        msg = await claim(IN_QUEUE, CONSUMER)
        if msg is None:
            try:
                await asyncio.wait_for(wait_for_message(IN_QUEUE, timeout_secs=5.0), 6.0)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await svc.handle_intent(msg)
            await ack(msg.msg_id)
        except Exception as e:
            log.error("intent failed", extra=kv(msg_id=msg.msg_id,
                                                error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def engine_loop(svc: C4Service, engine, marketdata, stop: asyncio.Event,
                      deadman_cfg: dict, exit_cfg: dict,
                      interval_secs: float = 60.0) -> None:
    """Per-minute during RTH: bars -> halt check -> engine.step per open
    position; overnight passes at 15:45/15:55 ET; dead-man + breaker every
    pass. Exit engine suspends (catastrophe stops sole protection) when the
    dead-man says marketdata is too stale to trust synthetic layers."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from a3_risk.service import minutes_to_close
    from .breaker import check_breaker
    from .deadman import check as deadman_check
    from .flags import get_flag
    from .state import open_positions

    ET = ZoneInfo("America/New_York")
    overnight_done: dict[str, str] = {}          # date -> last pass label

    while not stop.is_set():
        now = svc.now_fn()
        in_session = minutes_to_close(now) is not None
        try:
            await deadman_check(deadman_cfg["components"] and deadman_cfg,
                                now, in_session)
            await check_breaker(float(svc.cfg["drawdown_breaker_pct"]))
            await set_health("exec", "OK", "engine loop")

            if in_session and (await get_flag("exit_engine_suspended")) != "1":
                for pos in await open_positions():
                    if await engine.check_halt(pos):
                        continue
                    end = now
                    start = end - timedelta(minutes=3)
                    bars = await marketdata.minute_bars(pos["ticker"], start, end)
                    if not bars:
                        continue                  # halt heuristic accumulates
                    b = bars[-1]
                    await engine.step(pos, b)

                et = now.astimezone(ET)
                today = et.date().isoformat()
                hhmm = et.strftime("%H:%M")
                oc = exit_cfg["overnight_rule"]
                if hhmm >= "15:55" and overnight_done.get(today) == "15:45":
                    await engine.overnight_pass(oc, pass_label="15:55")
                    overnight_done[today] = "15:55"
                elif hhmm >= oc.get("check_time_et", "15:45") \
                        and today not in overnight_done:
                    await engine.overnight_pass(oc, pass_label="15:45")
                    overnight_done[today] = "15:45"
            else:
                # after the close, once: session-tf MIP predicates evaluate
                # on the finished session bar
                et = now.astimezone(ET)
                today = et.date().isoformat()
                if et.strftime("%H:%M") >= "16:01" \
                        and overnight_done.get(today) != "session_close":
                    async def _daily(ticker):
                        bars = await marketdata.daily_bars(ticker, 1)
                        return bars[-1] if bars else None
                    await engine.session_close_pass(_daily)
                    overnight_done[today] = "session_close"
        except Exception as e:
            log.error("engine loop error", extra=kv(error=repr(e)[:300]))
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_secs)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    from common.marketdata import get_marketdata
    from .engine import PositionEngine

    cfg = load_yaml(config_path("deadman.yaml"))
    exit_cfg = load_yaml(config_path("exit_profiles.yaml"))
    await register_config_version("c4 exec service startup")
    await ensure_defaults()
    svc = C4Service(cfg)
    engine = PositionEngine(
        svc.broker, now_fn=svc.now_fn,
        unprotected_max_secs=float(cfg["c4"]["exit_unprotected_max_secs"]))
    marketdata = get_marketdata()
    # reconciliation gate: NO intents accepted before this completes
    await reconcile(svc.broker)
    log.info("C4 up (reconciled)", extra=kv(consumer=CONSUMER))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def periodic_reconcile():
        interval = float(svc.cfg.get("reconcile_interval_min", 15)) * 60
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                try:
                    await reconcile(svc.broker)
                except Exception as e:
                    log.error("periodic reconcile failed",
                              extra=kv(error=repr(e)[:200]))
                    await set_health("broker_api", "DEGRADED", repr(e)[:200])

    recon_task = asyncio.create_task(periodic_reconcile())
    eng_task = asyncio.create_task(
        engine_loop(svc, engine, marketdata, stop, cfg, exit_cfg))
    await consume_loop(svc, stop)
    recon_task.cancel()
    eng_task.cancel()
    await set_health("exec", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

