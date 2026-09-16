"""1% snipe research: for each detection (scanner candidate or gate verdict) in the
last 30 days, fetch 1-minute bars and find which comes first after the detection
minute: +1% favourable or -1% adverse (direction = move sign for scanner rows,
thesis direction for gate rows). Also +0.5%/-0.5% and 60-min horizon."""
import asyncio, os, json, os, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
sys.path.insert(0, "/opt/pipeline/src")
from common.db import get_pool, close_pool
from common.marketdata import AlpacaData

CT = ZoneInfo("America/Chicago")
OUT = os.environ.get("RESEARCH_DIR", "/tmp/research") + "/onepct.json"

async def load():
    pool = await get_pool()
    async with pool.connection() as c:
        cur = await c.execute("""
            SELECT 'scanner' AS src, status, ticker, ts, (metrics->>'price')::float,
                   (metrics->>'move_pct')::float, (metrics->>'rel_volume')::float,
                   (metrics->>'minutes_since_hod')::float, (metrics->>'minutes_since_lod')::float,
                   (metrics->>'spread_bps')::float, (metrics->>'adv20_dollars')::float, NULL::text
            FROM journal.scanner_candidates
            WHERE ts >= now() - interval '30 days' AND metrics ? 'move_pct' AND metrics ? 'price'
              AND abs((metrics->>'move_pct')::float) >= 0.03""")
        scan = await cur.fetchall()
        cur = await c.execute("""
            SELECT 'gate' AS src, rule||'/'||veto_reason, ticker, veto_ts, price_at_veto::float,
                   pct_move_at_veto::float, vol_mult_at_veto::float, NULL, NULL, NULL, NULL, direction
            FROM journal.gate_counterfactuals
            WHERE veto_ts >= now() - interval '30 days' AND price_at_veto > 0""")
        gate = await cur.fetchall()
    return scan + gate

def first_hit(bars, t0, px, m, up, dn, horizon_min):
    """bars after t0 (start minute > t0 minute). Returns ('fav'|'adv'|'none', minutes)."""
    end = t0 + timedelta(minutes=horizon_min)
    for b in bars:
        if b["ts"] <= t0.replace(second=0, microsecond=0):
            continue
        if b["ts"] > end:
            break
        fav = (b["high"] / px - 1) if m > 0 else (1 - b["low"] / px)
        adv = (1 - b["low"] / px) if m > 0 else (b["high"] / px - 1)
        hit_f, hit_a = fav >= up, adv >= dn
        mins = (b["ts"] - t0).total_seconds() / 60
        if hit_f and hit_a:
            return "both", mins          # same bar: ambiguous, count as adverse
        if hit_a:
            return "adv", mins
        if hit_f:
            return "fav", mins
    return "none", horizon_min

async def main():
    rows = await load()
    md = AlpacaData()
    cache = {}
    out = []
    keys = sorted({(r[2], r[3].astimezone(CT).date()) for r in rows})
    print(f"{len(rows)} detections over {len(keys)} ticker-days", flush=True)
    sem = asyncio.Semaphore(6)
    async def fetch(t, d):
        async with sem:
            s = datetime(d.year, d.month, d.day, 8, 30, tzinfo=CT).astimezone(timezone.utc)
            e = datetime(d.year, d.month, d.day, 15, 0, tzinfo=CT).astimezone(timezone.utc)
            try:
                raw = await md.minute_bars(t, s, e)
            except Exception as ex:
                raw = []
            cache[(t, d)] = [{"ts": b["ts"].astimezone(CT), "high": b["high"], "low": b["low"], "close": b["close"], "volume": b["volume"]} for b in raw]
    await asyncio.gather(*(fetch(t, d) for t, d in keys))
    for r in rows:
        src, status, t, ts, px, mv, rv, mhod, mlod, spr, adv, direction = r
        t0 = ts.astimezone(CT)
        bars = cache.get((t, t0.date()), [])
        if not bars or not px:
            continue
        m = 1 if (direction == "up" if src == "gate" else (mv or 0) > 0) else -1
        if src == "gate" and direction not in ("up", "down"):
            continue
        rec = {"src": src, "status": status, "ticker": t, "ts": t0.isoformat(), "px": px, "move": mv,
               "relvol": rv, "m_hod": mhod, "m_lod": mlod, "spread": spr, "adv20": adv, "dir": m,
               "hr": t0.hour + t0.minute / 60}
        for up, dn, hz, name in ((0.01, 0.01, 30, "1v1_30"), (0.01, 0.01, 60, "1v1_60"), (0.01, 0.005, 60, "1v05_60"),
                                 (0.005, 0.005, 30, "05v05_30"), (0.01, 0.02, 60, "1v2_60")):
            rec[name] = first_hit(bars, t0, px, m, up, dn, hz)
        out.append(rec)
    json.dump(out, open(OUT, "w"))
    print(f"saved {len(out)} records", flush=True)
    await close_pool()

asyncio.run(main())
