"""A7 fact sheet — every number in the EOD report, computed by SQL/code
(baseline rule 5: numbers by code, narrative by model; the model NEVER
performs arithmetic on P&L).

The sheet covers one "report day": midnight ET to now. All queries are
read-only against the journal + news stores. Money values are floats in
dollars; R values are floats in R-multiples; everything is JSON-serializable
so the sheet rides in the decision payload for A11.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common.clock import utcnow
from common.db import get_pool

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")


def report_window(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """(start_utc, end_utc, session_date_str) for the ET calendar day."""
    now = now or utcnow()
    et_now = now.astimezone(ET)
    start = et_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(ZoneInfo("UTC")), now, et_now.strftime("%Y-%m-%d")


async def _rows(sql: str, *args) -> list[tuple]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


def _f(x) -> float | None:
    return float(x) if x is not None else None


async def build_facts(now: datetime | None = None) -> dict:
    start, end, session_date = report_window(now)

    # --- pipeline activity ---------------------------------------------------
    stage_counts = {}
    for stage, action, n in await _rows(
            """SELECT stage, action, count(*) FROM journal.decisions
               WHERE ts >= %s AND ts < %s GROUP BY 1, 2""", start, end):
        stage_counts.setdefault(stage, {})[action] = int(n)

    vetoes = [{"stage": s, "reason": r or "?", "count": int(n)}
              for s, r, n in await _rows(
            """SELECT stage, veto_reason, count(*) FROM journal.decisions
               WHERE action='VETO' AND ts >= %s AND ts < %s
               GROUP BY 1, 2 ORDER BY 3 DESC""", start, end)]

    items_ingested = int((await _rows(
        """SELECT count(*) FROM news.news_items
           WHERE received_ts >= %s AND received_ts < %s""", start, end))[0][0])

    # --- trades --------------------------------------------------------------
    opened = [{"position_id": pid, "ticker": t, "horizon": h, "qty": int(q),
               "avg_entry": _f(e), "initial_stop": _f(st),
               "opened_ts": ts.isoformat(), "headline": hl}
              for pid, t, h, q, e, st, ts, hl in await _rows(
            """SELECT p.position_id, p.ticker, p.horizon, p.qty_initial,
                      p.avg_entry, p.initial_stop, p.opened_ts,
                      (SELECT headline FROM news.news_items n
                       WHERE n.item_id = p.item_id
                       ORDER BY revision DESC LIMIT 1)
               FROM journal.positions p
               WHERE p.opened_ts >= %s AND p.opened_ts < %s
               ORDER BY p.opened_ts""", start, end)]

    exits_today = [{"position_id": pid, "ticker": t, "layer": layer,
                    "qty": int(q), "price": _f(px), "realized_pnl": _f(pnl),
                    "r_multiple": _f(r), "is_partial": part,
                    "ts": ts.isoformat()}
                   for pid, t, layer, q, px, pnl, r, part, ts in await _rows(
            """SELECT e.position_id, p.ticker, e.exit_layer, e.qty, e.price,
                      e.realized_pnl, e.r_multiple, e.is_partial, e.ts
               FROM journal.exits e JOIN journal.positions p USING (position_id)
               WHERE e.ts >= %s AND e.ts < %s ORDER BY e.ts""", start, end)]

    pnl_by_layer = [{"layer": layer, "count": int(n), "realized_pnl": _f(pnl)}
                    for layer, n, pnl in await _rows(
            """SELECT exit_layer, count(*), sum(realized_pnl)
               FROM journal.exits WHERE ts >= %s AND ts < %s
               GROUP BY 1 ORDER BY 3""", start, end)]
    realized_pnl_today = round(sum(x["realized_pnl"] or 0.0
                                   for x in pnl_by_layer), 2)

    open_positions = []
    for row in await _rows(
            """SELECT position_id, ticker, horizon, qty_open, avg_entry,
                      r_unit, last_price, realized_pnl,
                      exit_policy->>'current_stop', exit_policy->>'stop_basis',
                      opened_ts
               FROM journal.positions WHERE status='OPEN'
               ORDER BY opened_ts"""):
        (pid, t, h, q, entry, r_unit, last, rpnl, stop, basis, ots) = row
        entry, r_unit, last = _f(entry), _f(r_unit), _f(last)
        unreal = (round((last - entry) * int(q), 2) if last else None)
        unreal_r = (round((last - entry) / r_unit, 2)
                    if (last and r_unit) else None)
        open_positions.append({
            "position_id": pid, "ticker": t, "horizon": h, "qty_open": int(q),
            "avg_entry": entry, "last_price": last,
            "unrealized_pnl": unreal, "unrealized_r": unreal_r,
            "current_stop": _f(stop), "stop_basis": basis,
            "realized_pnl_partial": _f(rpnl),
            "opened_ts": ots.isoformat()})

    orders_today = {role: int(n) for role, n in await _rows(
        """SELECT order_role, count(*) FROM journal.orders
           WHERE submitted_ts >= %s AND submitted_ts < %s
           GROUP BY 1""", start, end)}

    # --- guard activity (Phase 5) -------------------------------------------
    guard = [{"position_id": pid, "ticker": t, "thesis_intact": intact,
              "recommended_action": act, "urgency": urg,
              "ts": ts.isoformat()}
             for pid, t, intact, act, urg, ts in await _rows(
            """SELECT g.position_id, p.ticker, g.thesis_intact,
                      g.recommended_action, g.urgency, g.ts
               FROM journal.guard_ledger g
               JOIN journal.positions p USING (position_id)
               WHERE g.ts >= %s AND g.ts < %s ORDER BY g.ts""", start, end)]
    guard_alerts = int((await _rows(
        """SELECT count(*) FROM journal.decisions
           WHERE stage='GUARD' AND action='ALERT_ONLY'
             AND ts >= %s AND ts < %s""", start, end))[0][0])

    # --- controls / system state --------------------------------------------
    controls = {k: v for k, v in await _rows(
        "SELECT key, value FROM journal.control")}
    health_bad = [{"component": c, "status": s, "detail": d}
                  for c, s, d in await _rows(
            """SELECT component, status, detail FROM journal.health
               WHERE status <> 'OK' ORDER BY component""")]
    gaps = [{"source": s, "start": gs.isoformat(),
             "end": ge.isoformat() if ge else None, "detail": d}
            for s, gs, ge, d in await _rows(
            """SELECT source, gap_start, gap_end, detail
               FROM news.ingestion_gaps
               WHERE gap_start >= %s OR gap_end IS NULL""", start)]
    quarantined_today = int((await _rows(
        """SELECT count(*) FROM news.quarantine
           WHERE received_ts >= %s AND received_ts < %s""", start, end))[0][0])

    return {
        "session_date": session_date,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "activity": {"items_ingested": items_ingested,
                     "stage_counts": stage_counts,
                     "vetoes": vetoes,
                     "quarantined_today": quarantined_today},
        "trades": {"opened": opened, "exits": exits_today,
                   "orders_by_role": orders_today,
                   "realized_pnl_today": realized_pnl_today,
                   "pnl_by_exit_layer": pnl_by_layer},
        "open_positions": open_positions,
        "guard": {"verdicts": guard, "alert_only_count": guard_alerts},
        "controls": controls,
        "health_not_ok": health_bad,
        "ingestion_gaps": gaps,
    }
