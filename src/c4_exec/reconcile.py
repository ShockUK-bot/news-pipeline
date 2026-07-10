"""C4 startup + periodic reconciliation (baseline v0.4/v0.5).

The broker is the source of truth, ALWAYS. On boot — BEFORE any intent is
accepted — and every reconcile_interval_min thereafter:

  1. Pull broker account, positions, open orders.
  2. Local OPEN position missing at broker  -> mark CLOSED_EXTERNAL
     (status CLOSED, RECONCILED event, audit row, alert health).
  3. Broker position missing locally        -> ADOPTED skeleton position row
     (no thesis lineage — operator review; audit + alert).
  4. Quantity drift                          -> local qty snapped to broker,
     RECONCILED event with old/new.
  5. Refresh capital rows in journal.control: broker_equity, settled_cash,
     last_reconcile_ts. Effective capital = min(broker_equity,
     trading_capital) is DERIVED by readers (A3, pre-flight) — never stored,
     never stale relative to an operator capital change.
"""
from __future__ import annotations

from common.broker import Broker
from common.clock import utcnow
from common.db import get_pool, jb
from common.journal import active_config_version
from common.log import get_logger, kv
from c1_ingestion.heartbeat import set_health

from .flags import set_flag
from .state import position_event

log = get_logger("c4.reconcile")


async def reconcile(broker: Broker) -> dict:
    account = await broker.get_account()
    broker_positions = {p.ticker: p for p in await broker.get_positions()}

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT position_id, ticker, qty_open, avg_entry
               FROM journal.positions WHERE status='OPEN'""")
        local = await cur.fetchall()

    summary = {"closed_external": [], "adopted": [], "qty_snapped": [],
               "equity": account.equity, "settled_cash": account.settled_cash}
    seen = set()

    async with pool.connection() as conn:
        async with conn.transaction():
            for position_id, ticker, qty_open, avg_entry in local:
                seen.add(ticker)
                bp = broker_positions.get(ticker)
                if bp is None or bp.qty <= 0:
                    await conn.execute(
                        """UPDATE journal.positions
                           SET status='CLOSED', closed_ts=now(), qty_open=0
                           WHERE position_id=%s""", (position_id,))
                    await position_event(position_id, "RECONCILED", "BROKER",
                                         old_value={"qty_open": qty_open},
                                         new_value={"qty_open": 0},
                                         detail="CLOSED_EXTERNAL: missing at broker",
                                         conn=conn)
                    await conn.execute(
                        """INSERT INTO journal.audit (actor, action, old_value,
                             new_value, detail)
                           VALUES ('C4','RECONCILE_CLOSED_EXTERNAL',%s,%s,%s)""",
                        (str(qty_open), "0", ticker))
                    summary["closed_external"].append(ticker)
                elif bp.qty != qty_open:
                    await conn.execute(
                        """UPDATE journal.positions SET qty_open=%s
                           WHERE position_id=%s""", (bp.qty, position_id))
                    await position_event(position_id, "RECONCILED", "BROKER",
                                         old_value={"qty_open": qty_open},
                                         new_value={"qty_open": bp.qty},
                                         detail="qty snapped to broker", conn=conn)
                    summary["qty_snapped"].append(ticker)

            for ticker, bp in broker_positions.items():
                if ticker in seen or bp.qty <= 0:
                    continue
                # ADOPTED skeleton: no thesis lineage; conservative synthetic
                # policy (operator must review). r_unit from a 2% notional stop.
                stop = round(bp.avg_entry * 0.98, 2)
                cur = await conn.execute(
                    """INSERT INTO journal.decisions
                       (signal_id, stage, agent, action, ticker, reason,
                        payload, config_version)
                       VALUES (%s,'ORDER','C4','ADOPTED',%s,
                               'position found at broker with no local record',
                               %s,%s)
                       RETURNING decision_id""",
                    (f"adopted:{ticker}:{utcnow().date()}", ticker,
                     jb({"qty": bp.qty, "avg_entry": bp.avg_entry}),
                     active_config_version()))
                dec_id = (await cur.fetchone())[0]
                cur = await conn.execute(
                    """INSERT INTO journal.intents
                       (intent_id, decision_id, ticker, side, qty, limit_price,
                        status, config_version)
                       VALUES (%s,%s,%s,'BUY',%s,%s,'FILLED',%s)
                       ON CONFLICT (intent_id) DO NOTHING""",
                    (f"adopted-{ticker}-{utcnow().date()}", dec_id, ticker,
                     bp.qty, bp.avg_entry, active_config_version()))
                cur = await conn.execute(
                    """INSERT INTO journal.positions
                       (ticker, horizon, profile, status, opened_ts,
                        entry_intent_id, thesis_decision_id, qty_initial,
                        qty_open, avg_entry, initial_stop, r_unit, exit_policy,
                        config_version)
                       VALUES (%s,'SHORT','adopted_v1','OPEN',now(),%s,%s,%s,
                               %s,%s,%s,%s,%s,%s)
                       RETURNING position_id""",
                    (ticker, f"adopted-{ticker}-{utcnow().date()}", dec_id,
                     bp.qty, bp.qty, bp.avg_entry, stop,
                     round(bp.avg_entry - stop, 4),
                     jb({"profile": "adopted_v1",
                         "initial_stop": {"method": "pct", "price": stop},
                         "note": "ADOPTED at reconciliation — operator review"}),
                     active_config_version()))
                pid = (await cur.fetchone())[0]
                await position_event(pid, "RECONCILED", "BROKER",
                                     new_value={"qty": bp.qty,
                                                "avg_entry": bp.avg_entry},
                                     detail="ADOPTED: broker position with no local record",
                                     conn=conn)
                await conn.execute(
                    """INSERT INTO journal.audit (actor, action, new_value, detail)
                       VALUES ('C4','RECONCILE_ADOPTED',%s,%s)""",
                    (str(bp.qty), ticker))
                summary["adopted"].append(ticker)

    await set_flag("broker_equity", f"{account.equity:.2f}", "C4",
                   "reconciliation refresh")
    await set_flag("settled_cash", f"{account.settled_cash:.2f}", "C4",
                   "reconciliation refresh")
    await set_flag("last_reconcile_ts", utcnow().isoformat(), "C4")

    status = "OK"
    detail = f"equity={account.equity:.0f}"
    if summary["closed_external"] or summary["adopted"]:
        status = "DEGRADED"
        detail = (f"drift: closed_external={summary['closed_external']} "
                  f"adopted={summary['adopted']}")
    await set_health("broker_api", status, detail)
    log.info("reconciled", extra=kv(**{k: v for k, v in summary.items()
                                       if k in ("equity", "closed_external",
                                                "adopted", "qty_snapped")}))
    return summary

