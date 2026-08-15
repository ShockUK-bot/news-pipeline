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
from common.direction import ENTRY_INTENT, entry_stop, r_unit as _r_unit
from common.journal import active_config_version
from common.log import get_logger, kv
from c1_ingestion.heartbeat import set_health

from .flags import set_flag
from .state import position_event

log = get_logger("c4.reconcile")


async def reconcile(broker: Broker, shorting_cfg: dict | None = None,
                    assets=None) -> dict:
    account = await broker.get_account()
    # v0.13: BrokerPosition is normalized upstream — qty always positive,
    # direction in .side (Alpaca reports shorts as negative qty).
    broker_positions = {p.ticker: p for p in await broker.get_positions()}

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT position_id, ticker, qty_open, avg_entry, side
               FROM journal.positions WHERE status='OPEN'""")
        local = await cur.fetchall()

    summary = {"closed_external": [], "adopted": [], "qty_snapped": [],
               "borrow_lost": [],
               "equity": account.equity, "settled_cash": account.settled_cash}
    seen = set()

    async with pool.connection() as conn:
        async with conn.transaction():
            for position_id, ticker, qty_open, avg_entry, side in local:
                side = side or "LONG"
                seen.add(ticker)
                bp = broker_positions.get(ticker)
                # v0.13: a broker position on the WRONG side is not ours —
                # treat like missing (drift alarm), never silently merge. A
                # vanished short may be a broker BUY-IN: journaled as such.
                if bp is None or bp.qty <= 0 or bp.side != side:
                    detail = "CLOSED_EXTERNAL: missing at broker"
                    if bp is not None and bp.qty > 0 and bp.side != side:
                        detail = (f"CLOSED_EXTERNAL: broker side {bp.side} != "
                                  f"local {side} — operator review")
                    elif side == "SHORT":
                        detail = ("CLOSED_EXTERNAL: short missing at broker "
                                  "(possible buy-in) — operator review")
                    await conn.execute(
                        """UPDATE journal.positions
                           SET status='CLOSED', closed_ts=now(), qty_open=0
                           WHERE position_id=%s""", (position_id,))
                    await position_event(position_id, "RECONCILED", "BROKER",
                                         old_value={"qty_open": qty_open},
                                         new_value={"qty_open": 0},
                                         detail=detail,
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
                # policy (operator must review). r_unit from a 2% notional
                # stop — BELOW entry for a long, ABOVE it for an adopted
                # short (v0.13).
                stop = entry_stop(bp.side, bp.avg_entry, bp.avg_entry * 0.02)
                cur = await conn.execute(
                    """INSERT INTO journal.decisions
                       (signal_id, stage, agent, action, ticker, reason,
                        payload, config_version)
                       VALUES (%s,'ORDER','C4','ADOPTED',%s,
                               'position found at broker with no local record',
                               %s,%s)
                       RETURNING decision_id""",
                    (f"adopted:{ticker}:{utcnow().date()}", ticker,
                     jb({"qty": bp.qty, "avg_entry": bp.avg_entry,
                         "side": bp.side}),
                     active_config_version()))
                dec_id = (await cur.fetchone())[0]
                cur = await conn.execute(
                    """INSERT INTO journal.intents
                       (intent_id, decision_id, ticker, side, qty, limit_price,
                        status, config_version)
                       VALUES (%s,%s,%s,%s,%s,%s,'FILLED',%s)
                       ON CONFLICT (intent_id) DO NOTHING""",
                    (f"adopted-{ticker}-{utcnow().date()}", dec_id, ticker,
                     ENTRY_INTENT[bp.side], bp.qty, bp.avg_entry,
                     active_config_version()))
                cur = await conn.execute(
                    """INSERT INTO journal.positions
                       (ticker, horizon, profile, status, opened_ts,
                        entry_intent_id, thesis_decision_id, qty_initial,
                        qty_open, avg_entry, initial_stop, r_unit, exit_policy,
                        config_version, side)
                       VALUES (%s,'SHORT','adopted_v1','OPEN',now(),%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s)
                       RETURNING position_id""",
                    (ticker, f"adopted-{ticker}-{utcnow().date()}", dec_id,
                     bp.qty, bp.qty, bp.avg_entry, stop,
                     _r_unit(bp.side, bp.avg_entry, stop),
                     jb({"profile": "adopted_v1", "side": bp.side,
                         "initial_stop": {"method": "pct", "price": stop},
                         "note": "ADOPTED at reconciliation — operator review"}),
                     active_config_version(), bp.side))
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
    # v0.13: margin buying power (clips SELL_SHORT entries in A3 + preflight)
    await set_flag("regt_buying_power", f"{account.regt_buying_power:.2f}",
                   "C4", "reconciliation refresh")
    await set_flag("last_reconcile_ts", utcnow().isoformat(), "C4")

    # v0.13: borrow re-check — an open short whose name has left the
    # easy-to-borrow list is flagged loudly (BORROW_LOST + DEGRADED health);
    # the operator decides (rule 12: auto-actions are risk-reducing only and
    # off by default). Best-effort: any error here must never break
    # reconciliation itself.
    if (shorting_cfg or {}).get("borrow_recheck", False):
        try:
            open_shorts = [(pid, t) for pid, t, q, a, s in local
                           if (s or "LONG") == "SHORT" and t not in
                           summary["closed_external"]]
            if open_shorts and assets is None:
                from common.assets import AssetsClient
                assets = AssetsClient(ttl_secs=int(
                    (shorting_cfg or {}).get("assets_ttl_secs", 1800)))
            for pid, t in open_shorts:
                info = await assets.get(t)
                if not info.etb_shortable:
                    await position_event(
                        pid, "BORROW_LOST", "C4",
                        new_value={"shortable": info.shortable,
                                   "easy_to_borrow": info.easy_to_borrow},
                        detail=f"{t} no longer ETB-shortable — operator "
                               f"review (buy-in risk)")
                    summary["borrow_lost"].append(t)
        except Exception as e:                                # noqa: BLE001
            log.warning("borrow re-check failed", extra=kv(error=repr(e)[:150]))

    status = "OK"
    detail = f"equity={account.equity:.0f}"
    if summary["closed_external"] or summary["adopted"] or summary["borrow_lost"]:
        status = "DEGRADED"
        detail = (f"drift: closed_external={summary['closed_external']} "
                  f"adopted={summary['adopted']} "
                  f"borrow_lost={summary['borrow_lost']}")
    await set_health("broker_api", status, detail)
    log.info("reconciled", extra=kv(**{k: v for k, v in summary.items()
                                       if k in ("equity", "closed_external",
                                                "adopted", "qty_snapped")}))
    return summary

