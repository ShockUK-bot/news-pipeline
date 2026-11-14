"""Two year test of the scanner's own entry (liquid names moving 4%+ vs the
previous close), FOLLOW and FADE, on 5 minute Alpaca bars. READ ONLY: one
journal SELECT for the universe, no writes. Output: one JSON line per event.

Triggers (first hit per name per day, bar END between 09:45 and 15:15 ET):
  A "move":    |close / prev_close - 1| >= 4%, price >= 5, ADV20 >= 25M
  B "scanner": A plus rel volume >= 3x (2x when ADV20 >= 1B) and the extreme
               (high for up movers, low for down) set within the last 60 min
Entry: OPEN of the next 5 minute bar (no look ahead).
Exits, both sides: L live ladder (2 ATR stop, breakeven at 0.75R, 3 ATR trail
from 1R, flat 15:50 ET); S 2 ATR stop then flat 15:50; H hold to 15:50.
"""
import asyncio, json, os, sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import httpx

sys.path.insert(0, "/opt/pipeline/src")
ET = ZoneInfo("America/New_York")
BASE = "https://data.alpaca.markets"
H = {"APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}
FEED = os.environ.get("ALPACA_FEED", "sip")
OUT = sys.argv[1]
START, END = "2024-10-01", "2026-11-14"
MIN_MOVE, MIN_PX, MIN_ADV, BIG_ADV = 0.04, 5.0, 25e6, 1e9
STOP_K, BE_R, TRAIL_K, TRAIL_AT = 2.0, 0.75, 3.0, 1.0


async def fetch(client, symbols):
    out, token = defaultdict(list), None
    while True:
        p = {"symbols": ",".join(symbols), "timeframe": "5Min", "start": START, "end": END,
             "limit": 10000, "adjustment": "split", "feed": FEED}
        if token:
            p["page_token"] = token
        for attempt in range(6):
            try:
                r = await client.get(f"{BASE}/v2/stocks/bars", params=p)
                if r.status_code == 429 or r.status_code >= 500:
                    await asyncio.sleep(5 * (attempt + 1)); continue
                r.raise_for_status(); break
            except httpx.HTTPError:
                await asyncio.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError("bars fetch failed")
        d = r.json()
        for s, bs in (d.get("bars") or {}).items():
            for b in bs:
                t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
                hm = t.hour * 60 + t.minute
                if 570 <= hm < 960:
                    out[s].append((t.date(), hm, b["o"], b["h"], b["l"], b["c"], b["v"]))
        token = d.get("next_page_token")
        await asyncio.sleep(0.25)
        if not token:
            return out


def by_day(bs):
    days = defaultdict(list)
    for b in bs:
        days[b[0]].append(b)
    return [days[d] for d in sorted(days)]


def sim(bars, side, entry, atr, ladder):
    """bars: the entry bar onward, same day. Returns (r, exit_reason)."""
    m = 1 if side == "L" else -1
    R = STOP_K * atr
    stop = entry - m * R
    best = entry
    last_c = entry
    for b in bars:
        _, hm, o, h, l, c, _ = b
        if hm >= 950:                                   # 15:50 ET: flat at the last close seen
            return m * (last_c - entry) / R, "FLAT"
        adverse, fav = (l, h) if m == 1 else (h, l)
        if m * (adverse - stop) <= 0:                   # stop touched (gap through: fill at the open)
            px = o if m * (o - stop) <= 0 else stop
            return m * (px - entry) / R, "STOP"
        if m * (fav - best) > 0:
            best = fav
        if ladder:
            prog = m * (best - entry) / R
            if prog >= BE_R and m * (entry - stop) > 0:
                stop = entry
            if prog >= TRAIL_AT:
                t = best - m * TRAIL_K * atr
                if m * (t - stop) > 0:
                    stop = t
        last_c = c
    return m * (last_c - entry) / R, "FLAT"


def events_for(sym, bs, qqq):
    days = by_day(bs)
    flat = [b for d in days for b in d]
    pos = {}
    k = 0
    for d in days:
        pos[d[0][0]] = k; k += len(d)
    dvol = [sum(b[5] * b[6] for b in d) for d in days]
    out = []
    for i in range(20, len(days)):
        day = days[i]; date = day[0][0]
        prev_close = days[i - 1][-1][5]
        adv = sum(dvol[i - 20:i]) / 20
        if adv < MIN_ADV or prev_close < MIN_PX:
            continue
        need_rv = 2.0 if adv >= BIG_ADV else 3.0
        done = set()
        cum = 0.0; hi = (-1e18, 0); lo = (1e18, 0)
        for j, b in enumerate(day):
            _, hm, o, h, l, c, v = b
            cum += v
            if h > hi[0]: hi = (h, hm)
            if l < lo[0]: lo = (l, hm)
            end = hm + 5
            if end < 585 or end > 915 or len(done) == 2:
                continue
            move = c / prev_close - 1
            if abs(move) < MIN_MOVE or c < MIN_PX or j + 1 >= len(day) or day[j + 1][1] >= 950:
                continue
            # rel volume: today's cumulative volume vs the prior 20 days at this time
            base = []
            for dd in days[i - 20:i]:
                base.append(sum(x[6] for x in dd if x[1] <= hm))
            avgc = sum(base) / len(base) if base else 0
            rv = cum / avgc if avgc > 0 else 0.0
            ext_hm = hi[1] if move > 0 else lo[1]
            fresh = (hm - ext_hm) <= 60
            trig = []
            if "A" not in done:
                trig.append("A")
            if "B" not in done and rv >= need_rv and fresh:
                trig.append("B")
            if not trig:
                continue
            gi = pos[date] + j
            win = flat[max(0, gi - 13):gi + 1]
            atr = sum(x[3] - x[4] for x in win) / len(win)
            entry = day[j + 1][2]
            if atr <= 0 or entry <= 0:
                continue
            rest = day[j + 1:]
            q = qqq.get(date)
            qmove = None
            if q:
                qc = [x[1] for x in q["bars"] if x[0] <= hm]
                if qc and q["prev"]:
                    qmove = qc[-1] / q["prev"] - 1
            hold_px = [x[5] for x in rest if x[1] < 950]
            hold = (hold_px[-1] / entry - 1) if hold_px else 0.0
            ev = {"sym": sym, "day": str(date), "hm": end, "move": round(move, 4), "rv": round(rv, 2),
                  "fresh": fresh, "adv": round(adv), "px": entry, "runit": round(STOP_K * atr / entry, 5),
                  "qqq": None if qmove is None else round(qmove, 4), "hold_long_pct": round(hold, 5)}
            for side in ("L", "S"):
                for name, lad in (("lad", True), ("stp", False)):
                    r, why = sim(rest, side, entry, atr, lad)
                    ev[f"{side}_{name}"] = round(r, 3); ev[f"{side}_{name}_x"] = why
            for t in trig:
                done.add(t)
                out.append({**ev, "trig": t})
    return out


async def main():
    from common.db import get_pool, close_pool
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("""
            SELECT ticker FROM journal.scanner_candidates WHERE metrics ? 'adv20_dollars'
              AND ticker NOT IN (SELECT DISTINCT ticker FROM journal.scanner_candidates
                                 WHERE reject_reason IN ('ETF_EXCLUDED','INSTRUMENT_SHAPE'))
            GROUP BY 1 HAVING max((metrics->>'adv20_dollars')::numeric) >= 25000000 ORDER BY 1""")
        uni = [r[0] for r in await cur.fetchall()]
    await close_pool()
    n = 0
    with open(OUT, "w") as f:
        async with httpx.AsyncClient(timeout=60.0, headers=H) as client:
            idx = await fetch(client, ["QQQ"])
            qd = by_day(idx["QQQ"])
            qqq = {}
            for i in range(1, len(qd)):
                qqq[qd[i][0][0]] = {"prev": qd[i - 1][-1][5], "bars": [(b[1], b[5]) for b in qd[i]]}
            print(f"universe {len(uni)}, QQQ sessions {len(qqq)}", flush=True)
            for i in range(0, len(uni), 8):
                batch = uni[i:i + 8]
                try:
                    data = await fetch(client, batch)
                except Exception as exc:                 # noqa: BLE001
                    print(f"batch {batch} failed: {exc!r}", flush=True); continue
                for s, bs in data.items():
                    for ev in events_for(s, bs, qqq):
                        f.write(json.dumps(ev) + "\n"); n += 1
                f.flush()
                print(f"{min(i + 8, len(uni))}/{len(uni)} names, {n} events", flush=True)
    print("DONE", n, flush=True)

asyncio.run(main())
