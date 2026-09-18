"""A11 scanner funnel counterfactuals (v0.23.0). For every scanner candidate
that was emitted or capped but never traded, replay a scalp_v1 trade from the
detection minute in BOTH directions on Alpaca minute bars and journal it.

Answers, with evidence instead of anecdotes: does the concurrency cap cost
money (CAPPED_CONCURRENT rows), and should the analyst be shorting scanner
up-moves (GATE_VETO SCANNER_STRUCTURE rows where analyst_direction is the
opposite of move_direction: what did the with-move trade do?).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from common.log import get_logger, kv

log = get_logger("a11.funnel")
LANE_NOTIONAL = 29_000.0        # about what the scanner lane sizes to since v0.16.0


def _tools():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p = os.path.join(root, "ops", "tools")
    if p not in sys.path:
        sys.path.insert(0, p)
    import scalp_replay
    return scalp_replay


def classify(status: str, reject_reason: Optional[str], decisions: list[tuple]) -> tuple[str, Optional[str]]:
    """(outcome, reason) from the candidate row and its decision chain
    [(stage, action, veto_reason, reason)] in time order. Pure."""
    if status == "CAPPED":
        return f"CAPPED_{reject_reason or 'UNKNOWN'}", None
    for stage, action, veto, reason in decisions:
        if stage == "ANALYST" and action == "REJECT":
            return "ANALYST_REJECT", (reason or "")[:160]
        if stage == "GATE" and action == "VETO":
            return "GATE_VETO", veto
        if stage == "RISK" and action == "VETO":
            return "RISK_VETO", veto
        if stage == "RISK" and action == "SHADOW_SHORT":
            return "SHADOW_SHORT", None
    return "NO_DECISION", None


async def pending(conn, days: int) -> list[dict]:
    cur = await conn.execute(
        """SELECT c.candidate_id, c.scan_date, c.ticker, c.status, c.reject_reason, c.item_id,
                  COALESCE((c.metrics->>'detected_ts')::timestamptz, c.ts) AS detect_ts,
                  (c.metrics->>'move_pct')::numeric AS move_pct
           FROM journal.scanner_candidates c
           WHERE c.scan_date >= current_date - %s AND c.status IN ('EMITTED','CAPPED')
             AND c.scan_date < current_date + 1
             AND NOT EXISTS (SELECT 1 FROM journal.positions p
                             WHERE p.origin='scanner' AND p.ticker=c.ticker AND p.opened_ts::date=c.scan_date)
             AND NOT EXISTS (SELECT 1 FROM journal.scanner_funnel_cf f WHERE f.candidate_id=c.candidate_id)
           ORDER BY c.ts""", (int(days),))
    cols = ("candidate_id", "scan_date", "ticker", "status", "reject_reason", "item_id", "detect_ts", "move_pct")
    return [dict(zip(cols, r)) for r in await cur.fetchall()]


async def chain(conn, item_id: Optional[str]) -> tuple[list[tuple], Optional[str]]:
    if not item_id:
        return [], None
    cur = await conn.execute(
        """SELECT stage, action, veto_reason, left(reason, 200),
                  COALESCE(payload->'thesis'->>'direction', payload->>'direction')
           FROM journal.decisions WHERE item_id=%s AND stage IN ('ANALYST','GATE','RISK')
           ORDER BY ts""", (item_id,))
    rows = await cur.fetchall()
    direction = next((r[4] for r in rows if r[0] == "ANALYST" and r[4]), None)
    return [(r[0], r[1], r[2], r[3]) for r in rows], direction


async def process(conn, cand: dict, dry: bool = False) -> Optional[dict]:
    sr = _tools()
    decisions, adir = await chain(conn, cand.get("item_id"))
    outcome, reason = classify(cand["status"], cand.get("reject_reason"), decisions)
    detect = cand["detect_ts"].astimezone(sr.CT)
    bars = await sr.fetch_bars(cand["ticker"], detect)
    if not bars:
        return None
    first = next((b for b in bars if b["ts"] >= detect.replace(second=0, microsecond=0)), None)
    if first is None:
        return None
    entry = float(first["open"])
    atr = sr.atr5m_before(bars, detect) or round(entry * 0.005, 4)
    qty = max(1, int(LANE_NOTIONAL / entry))
    L = sr.replay(bars, "LONG", detect, entry, qty, atr, 0.015, 2.0)
    S = sr.replay(bars, "SHORT", detect, entry, qty, atr, 0.015, 2.0)
    move_dir = "up" if float(cand.get("move_pct") or 0) >= 0 else "down"
    with_move = L["r"] if move_dir == "up" else S["r"]
    row = {"candidate_id": cand["candidate_id"], "scan_date": cand["scan_date"], "ticker": cand["ticker"],
           "detect_ts": cand["detect_ts"], "move_direction": move_dir, "outcome": outcome, "reason": reason,
           "analyst_direction": adir, "entry_px": entry, "atr_5m": atr,
           "long_r": L["r"], "long_pnl": L["pnl"], "short_r": S["r"], "short_pnl": S["pnl"],
           "with_move_r": with_move, "long_exits": L["exits"], "short_exits": S["exits"], "qty": qty}
    if not dry:
        await conn.execute(
            """INSERT INTO journal.scanner_funnel_cf
               (candidate_id, scan_date, ticker, detect_ts, move_direction, outcome, reason, analyst_direction,
                entry_px, atr_5m, long_r, long_pnl, short_r, short_pnl, with_move_r, long_exits, short_exits, qty)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (candidate_id) DO NOTHING""",
            (row["candidate_id"], row["scan_date"], row["ticker"], row["detect_ts"], move_dir, outcome, reason, adir,
             entry, atr, L["r"], L["pnl"], S["r"], S["pnl"], with_move, json.dumps(L["exits"]), json.dumps(S["exits"]), qty))
    return row


async def funnel_pass(conn, days: int = 14, dry: bool = False) -> list[dict]:
    out = []
    for cand in await pending(conn, days):
        try:
            r = await process(conn, cand, dry)
        except Exception as exc:                                  # noqa: BLE001
            log.warning("funnel replay failed", extra=kv(candidate_id=cand["candidate_id"], ticker=cand["ticker"],
                                                         error=repr(exc)[:120]))
            continue
        if r:
            out.append(r)
    return out


async def summary(conn, days: int = 30) -> dict:
    """The two questions, as numbers. Pure SQL over the journaled rows."""
    cur = await conn.execute("""
        SELECT outcome, count(*), round(sum(with_move_r),2), round(sum(long_r),2), round(sum(short_r),2),
               count(*) FILTER (WHERE with_move_r > 0)
        FROM journal.scanner_funnel_cf WHERE scan_date >= current_date - %s GROUP BY 1 ORDER BY 2 DESC""", (days,))
    by_outcome = {r[0]: {"n": r[1], "with_move_r": float(r[2]), "long_r": float(r[3]), "short_r": float(r[4]),
                         "with_move_winners": r[5]} for r in await cur.fetchall()}
    cur = await conn.execute("""
        SELECT count(*), round(sum(short_r),2), round(sum(long_r),2), count(*) FILTER (WHERE long_r > short_r)
        FROM journal.scanner_funnel_cf
        WHERE scan_date >= current_date - %s AND move_direction='up' AND analyst_direction='down'""", (days,))
    n, s_r, l_r, l_better = await cur.fetchone()
    return {"by_outcome": by_outcome,
            "analyst_short_on_up_move": {"n": n, "short_sum_r": float(s_r or 0), "long_sum_r": float(l_r or 0),
                                         "long_better": l_better}}
