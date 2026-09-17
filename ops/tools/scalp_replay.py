#!/usr/bin/env python
"""scalp_replay — replay the scalp_v1 exit ladder on 1-minute bars (v0.14.6).

Answers "what would this trade have done with a different stop or a later
entry?" using the same bars C4 sees live (Alpaca 1-minute, SIP feed) and the
same ladder as src/c4_exec/exits.py: synthetic stop on the losing edge, then
time stop (60 min, < 0.5R), then 50% scale-out at entry x (1 + 0.6 x
magnitude_est), then breakeven at 0.75R and a 1.5 x ATR(5m) trail from 1R,
tighten-only, force-flat 15:50 ET. Fills are assumed AT the stop / target
price or the bar close; real slippage is journaled separately on the exit
event (slip_px / slip_r) since v0.14.6.

Usage (from /opt/pipeline, env sourced, PYTHONPATH=src):

    .venv/bin/python ops/tools/scalp_replay.py --position-id 43
    .venv/bin/python ops/tools/scalp_replay.py --ticker DELL --date 2026-09-11 \
        --entry-time 08:57:19 --entry-price 551.35 --qty 26
    .venv/bin/python ops/tools/scalp_replay.py --ticker OKLO --date 2026-09-11 \
        --side SHORT --entry-time 08:54 --atr-k 2.0,3.0 --entry-times 09:15,09:30

--position-id reads entry, qty, side, ATR(5m) and magnitude_est from
journal.positions (read only). --ticker mode takes them from flags; ATR(5m)
defaults to the mean true range of the 14 five-minute bars before entry and
magnitude_est to 0.015 when not given. Times are America/Chicago. Alternatives
are one row per ATR multiple (same entry) plus one per alternative entry time
(2.0 ATR). Read only: no orders, no journal writes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
TF, BE_R, TRAIL_K, TRAIL_AT, TIME_WIN, TIME_MIN_R = 0.6, 0.75, 1.5, 1.0, 60, 0.5
FORCE_FLAT_CT = "14:50"


def replay(bars: list[dict], side: str, entry_ts: datetime, entry_px: float,
           qty: int, atr: float, mag: float, stop_k: float,
           trail_k: float = TRAIL_K, time_stop: bool = True,
           scale_out: bool = True, runner_hold: bool = False) -> dict:
    """Pure ladder replay. bars: [{ts(CT datetime), open, high, low, close}].
    v0.16.0 variants: trail_k (ATR multiple of the trail), time_stop off,
    scale_out off (full size rides the trail), runner_hold (after the scale-out
    the runner keeps only the breakeven stop until force-flat)."""
    m = 1 if side == "LONG" else -1
    stop = round(entry_px - m * stop_k * atr, 2)
    r_unit = stop_k * atr
    target = round(entry_px * (1 + m * TF * mag), 4)
    hwm, basis, qty_open, scaled, pnl, exits = entry_px, "initial", qty, False, 0.0, []
    mfe, close_r = 0.0, None
    first = entry_ts.replace(second=0, microsecond=0)
    for b in bars:
        if b["ts"] < first:
            continue
        hhmm = b["ts"].strftime("%H:%M")
        minutes_open = (b["ts"] - entry_ts).total_seconds() / 60
        lose = b["low"] if side == "LONG" else b["high"]
        win = b["high"] if side == "LONG" else b["low"]
        mfe = max(mfe, m * (win - entry_px) / r_unit)
        if (lose <= stop) if side == "LONG" else (lose >= stop):
            layer = {"initial": "STOP", "breakeven": "BREAKEVEN", "trail": "TRAIL"}[basis]
            pnl += m * (stop - entry_px) * qty_open
            exits.append((hhmm, layer, qty_open, stop))
            qty_open = 0
            break
        prog = m * (b["close"] - entry_px) / r_unit
        if hhmm >= FORCE_FLAT_CT:
            pnl += m * (b["close"] - entry_px) * qty_open
            exits.append((hhmm, "FORCE_FLAT", qty_open, b["close"]))
            qty_open = 0
            break
        if time_stop and minutes_open >= TIME_WIN and prog < TIME_MIN_R:
            pnl += m * (b["close"] - entry_px) * qty_open
            exits.append((hhmm, "TIME", qty_open, b["close"]))
            qty_open = 0
            break
        if not scaled and ((win >= target) if side == "LONG" else (win <= target)):
            half = qty_open // 2 if scale_out else 0
            if half > 0:
                pnl += m * (target - entry_px) * half
                qty_open -= half
                exits.append((hhmm, "TARGET", half, target))
            scaled = True
        hwm = max(hwm, win) if side == "LONG" else min(hwm, win)
        proposed = None
        if runner_hold and scaled:
            if basis == "initial":
                proposed = (round(entry_px, 2), "breakeven")
        elif prog >= TRAIL_AT:
            proposed = (round(hwm - m * trail_k * atr, 2), "trail")
        elif prog >= BE_R and basis == "initial":
            proposed = (round(entry_px, 2), "breakeven")
        if proposed and ((proposed[0] > stop) if side == "LONG" else (proposed[0] < stop)):
            stop, basis = proposed
    if qty_open and bars:
        last = bars[-1]["close"]
        pnl += m * (last - entry_px) * qty_open
        exits.append((bars[-1]["ts"].strftime("%H:%M"), "EOD_MARK", qty_open, last))
    flat = next((b for b in bars if b["ts"].strftime("%H:%M") >= FORCE_FLAT_CT and b["ts"] >= first), None)
    if flat is not None and r_unit:
        close_r = round(m * (flat["close"] - entry_px) / r_unit, 3)
    return {"pnl": round(pnl, 2), "r": round(pnl / (qty * r_unit), 2) if qty and r_unit else 0.0,
            "r_unit": round(r_unit, 4), "stop0": stop if not exits else None, "exits": exits,
            "mfe_r": round(mfe, 3), "close_r": close_r}


VARIANTS = {                       # v0.16.0: journaled nightly per scanner trade
    "base": {},
    "noscale": {"scale_out": False},
    "runner_hold": {"runner_hold": True},
    "notime": {"time_stop": False},
    "stop3.0": {"stop_k": 3.0},
    "trail2.5": {"trail_k": 2.5},
    "trail3.0": {"trail_k": 3.0},
}


def replay_variants(bars, side, entry_ts, entry_px, qty, atr, mag) -> dict[str, dict]:
    out = {}
    for name, kw in VARIANTS.items():
        kw = dict(kw)
        k = kw.pop("stop_k", 2.0)
        out[name] = replay(bars, side, entry_ts, entry_px, qty, atr, mag, k, **kw)
    return out


def atr5m_before(bars: list[dict], entry_ts: datetime, n: int = 14) -> float | None:
    """Mean true range of the last n five-minute bars ending before entry."""
    fives, cur, key = [], None, None
    for b in bars:
        if b["ts"] >= entry_ts:
            break
        k = b["ts"].replace(minute=b["ts"].minute - b["ts"].minute % 5, second=0)
        if k != key:
            if cur:
                fives.append(cur)
            cur, key = dict(b), k
        else:
            cur["high"], cur["low"], cur["close"] = max(cur["high"], b["high"]), min(cur["low"], b["low"]), b["close"]
    if cur:
        fives.append(cur)
    fives = fives[-n:]
    if len(fives) < 3:
        return None
    trs = []
    for i, f in enumerate(fives):
        prev_close = fives[i - 1]["close"] if i else f["open"]
        trs.append(max(f["high"] - f["low"], abs(f["high"] - prev_close), abs(f["low"] - prev_close)))
    return round(sum(trs) / len(trs), 4)


async def load_position(position_id: int) -> dict:
    from common.db import get_pool
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT ticker, side, opened_ts, avg_entry, qty_initial, r_unit,
                      exit_policy, realized_pnl, closed_ts
               FROM journal.positions WHERE position_id = %s""", (position_id,))
        row = await cur.fetchone()
    if not row:
        sys.exit(f"position {position_id} not found")
    ticker, side, opened_ts, avg_entry, qty, r_unit, policy, realized, closed_ts = row
    if isinstance(policy, str):
        policy = json.loads(policy)
    # v0.16.0: a scalp promoted to short_term_v1 has its policy rewritten with
    # the DAILY atr, so derive ATR(5m) from the journaled R unit (2.0 x ATR at
    # entry for scalp_v1) instead of trusting the policy's atr fields.
    atr = float(r_unit) / 2.0 if r_unit else float(policy.get("atr_value") or policy["atr_14"])
    return {"ticker": ticker, "side": side, "entry_ts": opened_ts.astimezone(CT),
            "closed_ts": closed_ts.astimezone(CT) if closed_ts else None,
            "entry_px": float(avg_entry), "qty": int(qty), "r_unit": float(r_unit),
            "atr": atr,
            "mag": float(policy.get("magnitude_est") or 0.015),
            "actual_pnl": float(realized),
            "actual_r": round(float(realized) / (int(qty) * float(r_unit)), 2)}


async def fetch_bars(ticker: str, day: datetime) -> list[dict]:
    from common.marketdata import AlpacaData
    start = day.replace(hour=8, minute=30, tzinfo=CT).astimezone(timezone.utc)
    end = day.replace(hour=15, minute=0, tzinfo=CT).astimezone(timezone.utc)
    raw = await AlpacaData().minute_bars(ticker, start, end)
    return [{"ts": b["ts"].astimezone(CT), "open": b["open"], "high": b["high"],
             "low": b["low"], "close": b["close"]} for b in raw]


def fmt(r: dict) -> str:
    ex = "; ".join(f"{t} {layer} {q}@{px:.2f}" for t, layer, q, px in r["exits"])
    return f"{r['r']:+.2f}R {r['pnl']:+9.2f}  {ex}"


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--position-id", type=int)
    ap.add_argument("--ticker")
    ap.add_argument("--date", help="YYYY-MM-DD (America/Chicago session)")
    ap.add_argument("--entry-time", help="HH:MM[:SS] CT (ticker mode)")
    ap.add_argument("--entry-price", type=float, help="default: open of the entry minute")
    ap.add_argument("--side", default="LONG", choices=["LONG", "SHORT"])
    ap.add_argument("--qty", type=int, default=100)
    ap.add_argument("--atr", type=float, help="ATR(5m); default: measured from bars before entry")
    ap.add_argument("--magnitude", type=float, help="magnitude_est; default 0.015")
    ap.add_argument("--atr-k", default="2.0,3.0", help="stop multiples, same entry")
    ap.add_argument("--entry-times", default="09:15,09:30", help="alternative entries CT, at 2.0 ATR")
    args = ap.parse_args()

    if args.position_id:
        p = await load_position(args.position_id)
        day = p["entry_ts"]
    else:
        if not (args.ticker and args.date and args.entry_time):
            sys.exit("ticker mode needs --ticker, --date and --entry-time")
        day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=CT)
        parts = [int(x) for x in args.entry_time.split(":")]
        entry_ts = day.replace(hour=parts[0], minute=parts[1],
                               second=parts[2] if len(parts) > 2 else 0)
        p = {"ticker": args.ticker.upper(), "side": args.side, "entry_ts": entry_ts,
             "entry_px": args.entry_price, "qty": args.qty, "atr": args.atr,
             "mag": args.magnitude or 0.015, "actual_pnl": None, "actual_r": None}

    bars = await fetch_bars(p["ticker"], day)
    if not bars:
        sys.exit(f"no bars for {p['ticker']} on {day.date()}")
    if p["entry_px"] is None:
        first = next((b for b in bars if b["ts"] >= p["entry_ts"].replace(second=0)), None)
        if not first:
            sys.exit("entry time is after the last bar")
        p["entry_px"] = first["open"]
    if p["atr"] is None:
        p["atr"] = atr5m_before(bars, p["entry_ts"])
        if p["atr"] is None:
            sys.exit("cannot measure ATR(5m) before entry; pass --atr")

    print(f"{p['ticker']} {day.date()} {p['side']} entry {p['entry_px']:.2f} @ "
          f"{p['entry_ts'].strftime('%H:%M:%S')} CT qty {p['qty']} atr5m {p['atr']:.4f} "
          f"magnitude_est {p['mag']} ({len(bars)} bars)")
    if p["actual_r"] is not None:
        print(f"  actual         : {p['actual_r']:+.2f}R {p['actual_pnl']:+9.2f}")
    for k in [float(x) for x in args.atr_k.split(",") if x]:
        r = replay(bars, p["side"], p["entry_ts"], p["entry_px"], p["qty"], p["atr"], p["mag"], k)
        print(f"  {k:.1f} ATR same entry: {fmt(r)}")
    for hhmm in [x.strip() for x in args.entry_times.split(",") if x.strip()]:
        b = next((b for b in bars if b["ts"].strftime("%H:%M") == hhmm), None)
        if b is None:
            print(f"  enter {hhmm}     : no bar")
            continue
        if b["ts"] < p["entry_ts"]:
            note = "  (before the real entry: look-ahead if the signal was not yet detected)"
        else:
            note = ""
        r = replay(bars, p["side"], b["ts"], b["open"], p["qty"], p["atr"], p["mag"], 2.0)
        print(f"  enter {hhmm} px {b['open']:.2f}: {fmt(r)}{note}")


if __name__ == "__main__":
    if "src" not in os.environ.get("PYTHONPATH", "") and os.path.isdir("src"):
        sys.path.insert(0, "src")
    asyncio.run(main())
