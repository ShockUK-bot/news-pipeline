"""A13 retrieval packs — the ONLY data surface the chat model sees.

Every pack is a parameterized query written here in code. The planner call
picks pack names + parameters (schema-validated enum); nothing the model
emits is ever interpolated into SQL. Timestamps are ISO-ified and money is
stringified so the fact sheet is JSON-safe and the model can quote numbers
verbatim (rule 5: numbers computed by code, narrative by the model).
"""
from __future__ import annotations

import json
from typing import Any

from common.db import get_pool
from common.log import get_logger

from .schema import PlannedQuery

log = get_logger("a13.retrieval")


def _jsonable(rows: list[dict]) -> list[dict]:
    return json.loads(json.dumps(rows, default=str))


async def _rows(sql: str, params: tuple) -> list[dict]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        cols = [d.name for d in cur.description]
        return _jsonable([dict(zip(cols, r)) for r in await cur.fetchall()])


# ---------------------------------------------------------------------------
# Packs. Each returns a JSON-safe list/dict.
# ---------------------------------------------------------------------------

async def open_positions(ticker: str | None, limit: int) -> list[dict]:
    return await _rows(
        """SELECT p.position_id, p.ticker, p.horizon, p.profile, p.qty_open,
                  p.qty_initial, p.avg_entry, p.initial_stop, p.r_unit,
                  p.last_price, p.opened_ts, p.realized_pnl,
                  p.exit_policy->>'current_stop' AS current_stop,
                  LEFT(d.reason, 300)            AS thesis
           FROM journal.positions p
           JOIN journal.decisions d ON d.decision_id = p.thesis_decision_id
           WHERE p.status = 'OPEN' AND (%s::text IS NULL OR p.ticker = %s)
           ORDER BY p.opened_ts DESC LIMIT %s""",
        (ticker, ticker, limit))


async def closed_trades(ticker: str | None, days: int, limit: int) -> list[dict]:
    return await _rows(
        """SELECT p.position_id, p.ticker, p.horizon, p.profile,
                  p.qty_initial, p.avg_entry, p.opened_ts, p.closed_ts,
                  p.realized_pnl,
                  (SELECT e.exit_layer FROM journal.exits e
                   WHERE e.position_id = p.position_id
                   ORDER BY e.ts DESC LIMIT 1)   AS last_exit_layer,
                  (SELECT ROUND(SUM(e.r_multiple)::numeric, 3) FROM journal.exits e
                   WHERE e.position_id = p.position_id) AS realized_r
           FROM journal.positions p
           WHERE p.status = 'CLOSED'
             AND p.closed_ts > now() - make_interval(days => %s)
             AND (%s::text IS NULL OR p.ticker = %s)
           ORDER BY p.closed_ts DESC LIMIT %s""",
        (days, ticker, ticker, limit))


async def position_detail(position_id: int | None, ticker: str | None,
                          limit: int) -> dict:
    where, param = ("p.position_id = %s", position_id) if position_id else \
                   ("p.ticker = %s", ticker)
    pos = await _rows(
        f"""SELECT p.position_id, p.ticker, p.status, p.horizon, p.profile,
                   p.qty_initial, p.qty_open, p.avg_entry, p.initial_stop,
                   p.r_unit, p.last_price, p.opened_ts, p.closed_ts,
                   p.realized_pnl, p.exit_policy, p.item_id,
                   d.decision_id AS thesis_decision_id,
                   LEFT(d.reason, 400) AS thesis, d.confidence AS thesis_confidence
            FROM journal.positions p
            JOIN journal.decisions d ON d.decision_id = p.thesis_decision_id
            WHERE {where} ORDER BY p.opened_ts DESC LIMIT 1""",
        (param,))
    if not pos:
        return {"position": None}
    pid = pos[0]["position_id"]
    events = await _rows(
        """SELECT ts, event_type, actor, r_progress, detail
           FROM journal.position_events WHERE position_id = %s
           ORDER BY ts DESC LIMIT %s""", (pid, limit))
    exits = await _rows(
        """SELECT ts, exit_layer, qty, price, realized_pnl, r_multiple, is_partial
           FROM journal.exits WHERE position_id = %s ORDER BY ts""", (pid,))
    return {"position": pos[0], "events": events, "exits": exits}


async def vetoes(ticker: str | None, days: int, limit: int) -> list[dict]:
    return await _rows(
        """SELECT decision_id, ts, signal_id, stage, agent, ticker,
                  veto_reason, LEFT(reason, 300) AS reason, confidence
           FROM journal.decisions
           WHERE action = 'VETO'
             AND ts > now() - make_interval(days => %s)
             AND (%s::text IS NULL OR ticker = %s)
           ORDER BY ts DESC LIMIT %s""",
        (days, ticker, ticker, limit))


async def decision_trace(signal_id: str | None, ticker: str | None,
                         days: int, limit: int) -> list[dict]:
    if signal_id:
        return await _rows(
            """SELECT decision_id, ts, signal_id, stage, agent, action,
                      veto_reason, LEFT(reason, 300) AS reason, confidence,
                      latency_ms
               FROM journal.decisions WHERE signal_id = %s
               ORDER BY ts LIMIT %s""", (signal_id, limit))
    return await _rows(
        """SELECT decision_id, ts, signal_id, stage, agent, action,
                  veto_reason, LEFT(reason, 300) AS reason, confidence,
                  latency_ms
           FROM journal.decisions
           WHERE ticker = %s AND ts > now() - make_interval(days => %s)
           ORDER BY ts DESC LIMIT %s""", (ticker, days, limit))


async def ticker_news(ticker: str, days: int, limit: int) -> list[dict]:
    return await _rows(
        """SELECT item_id, revision, source, source_tier, headline,
                  LEFT(summary, 300) AS summary, published_ts, received_ts,
                  is_correction
           FROM news.news_items_latest
           WHERE %s = ANY(symbols)
             AND received_ts > now() - make_interval(days => %s)
           ORDER BY received_ts DESC LIMIT %s""",
        (ticker, days, limit))


async def ticker_snapshot(ticker: str, md=None) -> dict:
    """Live market context; degrades to an error note if market data is down
    (the answer must say so rather than invent numbers)."""
    out: dict[str, Any] = {"ticker": ticker}
    try:
        from common.marketdata import adv20, atr14, get_marketdata
        md = md or get_marketdata()
        quote = await md.snapshot(ticker)
        daily = await md.daily_bars(ticker, 30)
        out["last"] = quote.price
        out["prev_close"] = await md.prev_close(ticker)
        out["atr_14"] = atr14(daily)
        out["adv_20d"] = adv20(daily)
    except Exception as e:                               # noqa: BLE001
        out["marketdata_error"] = repr(e)[:200]
    regime = await _rows(
        """SELECT regime_id, ts, features FROM journal.regime_snapshots
           ORDER BY ts DESC LIMIT 1""", ())
    out["regime"] = regime[0] if regime else None
    open_pos = await _rows(
        """SELECT position_id, qty_open, avg_entry FROM journal.positions
           WHERE ticker = %s AND status = 'OPEN'""", (ticker,))
    out["open_position"] = open_pos[0] if open_pos else None
    return _jsonable([out])[0]


async def performance(days: int) -> list[dict]:
    return await _rows(
        """SELECT date_trunc('day', p.closed_ts)::date AS day,
                  COUNT(*)                             AS trades,
                  COUNT(*) FILTER (WHERE p.realized_pnl > 0) AS wins,
                  ROUND(SUM(p.realized_pnl)::numeric, 2)     AS realized_pnl,
                  ROUND(MAX(p.realized_pnl)::numeric, 2)     AS best,
                  ROUND(MIN(p.realized_pnl)::numeric, 2)     AS worst
           FROM journal.positions p
           WHERE p.status = 'CLOSED'
             AND p.closed_ts > now() - make_interval(days => %s)
           GROUP BY 1 ORDER BY 1 DESC""", (days,))


async def control_state() -> dict:
    control = await _rows("SELECT key, value, updated_ts FROM journal.control", ())
    health = await _rows(
        "SELECT component, status, detail, updated_ts FROM journal.health", ())
    return {"control": control, "health": health}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def run_queries(planned: list[PlannedQuery], cfg: dict, md=None) -> dict:
    """Execute the validated plan; returns {pack_name: result | {error}}.
    A failing pack degrades to an error entry — the answer call must surface
    it, never fabricate the missing data."""
    r = cfg.get("retrieval") or {}
    default_days = int(r.get("default_days", 30))
    news_days = int(r.get("news_days", 5))
    max_rows = int(r.get("max_rows", 40))
    max_queries = int(r.get("max_queries", 5))

    fact_sheet: dict[str, Any] = {}
    for q in planned[:max_queries]:
        days = q.days or default_days
        limit = min(q.limit or max_rows, max_rows)
        key = q.query if q.query not in fact_sheet else f"{q.query}:{q.ticker or q.position_id}"
        try:
            if q.query == "open_positions":
                fact_sheet[key] = await open_positions(q.ticker, limit)
            elif q.query == "closed_trades":
                fact_sheet[key] = await closed_trades(q.ticker, days, limit)
            elif q.query == "position_detail":
                if not (q.position_id or q.ticker):
                    fact_sheet[key] = {"error": "position_detail needs position_id or ticker"}
                else:
                    fact_sheet[key] = await position_detail(q.position_id, q.ticker, limit)
            elif q.query == "vetoes":
                fact_sheet[key] = await vetoes(q.ticker, days, limit)
            elif q.query == "decision_trace":
                if not (q.signal_id or q.ticker):
                    fact_sheet[key] = {"error": "decision_trace needs signal_id or ticker"}
                else:
                    fact_sheet[key] = await decision_trace(q.signal_id, q.ticker, days, limit)
            elif q.query == "ticker_news":
                if not q.ticker:
                    fact_sheet[key] = {"error": "ticker_news needs ticker"}
                else:
                    fact_sheet[key] = await ticker_news(q.ticker, q.days or news_days, limit)
            elif q.query == "ticker_snapshot":
                if not q.ticker:
                    fact_sheet[key] = {"error": "ticker_snapshot needs ticker"}
                else:
                    fact_sheet[key] = await ticker_snapshot(q.ticker, md=md)
            elif q.query == "performance":
                fact_sheet[key] = await performance(days)
            elif q.query == "control_state":
                fact_sheet[key] = await control_state()
        except Exception as e:                            # noqa: BLE001
            log.warning("retrieval pack failed",
                        extra={"kv": {"pack": q.query, "error": repr(e)[:200]}})
            fact_sheet[key] = {"error": f"pack failed: {repr(e)[:200]}"}
    return fact_sheet


def truncate_fact_sheet(fact_sheet: dict, max_chars: int) -> dict:
    """Trim list-valued packs until the serialized sheet fits the budget.
    Truncation is recorded in the sheet itself so the answer can say so."""
    def size(fs: dict) -> int:
        return len(json.dumps(fs, default=str))

    fs = dict(fact_sheet)
    truncated: list[str] = []
    while size(fs) > max_chars:
        candidates = [k for k in fs if k != "_truncated"
                      and isinstance(fs[k], list) and len(fs[k]) > 1]
        if not candidates:
            break
        longest = max(candidates, key=lambda k: len(json.dumps(fs[k], default=str)))
        fs[longest] = fs[longest][:max(1, len(fs[longest]) // 2)]
        if longest not in truncated:
            truncated.append(longest)
        fs["_truncated"] = truncated
    return fs
