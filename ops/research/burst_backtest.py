"""Burst backtest for the '1% gain' idea on 1-minute bars (liquid universe, ~21 sessions).
Signal uses only closed bars. Entry at next bar open. Bracket target/stop on
subsequent bar highs/lows (stop assumed hit first when both in one bar), time stop
exits at close. Cost = round-trip cost_bps applied to every trade."""
import asyncio, os, json, os, sys, statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import httpx

CT = ZoneInfo("America/Chicago")
S = os.environ.get("RESEARCH_DIR", "/tmp/research")
KEY, SEC = os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"]
H = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30

async def fetch_all(symbols):
    start = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%dT00:00:00Z")
    end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = defaultdict(list)
    async with httpx.AsyncClient(timeout=60, headers=H) as c:
        for i in range(0, len(symbols), 40):
            chunk = symbols[i:i+40]
            token = None
            while True:
                p = {"symbols": ",".join(chunk), "timeframe": "1Min", "start": start, "end": end,
                     "limit": 10000, "feed": "sip", "adjustment": "raw"}
                if token: p["page_token"] = token
                r = await c.get("https://data.alpaca.markets/v2/stocks/bars", params=p)
                r.raise_for_status()
                d = r.json()
                for sym, bars in (d.get("bars") or {}).items():
                    out[sym].extend(bars)
                token = d.get("next_page_token")
                if not token: break
            print(f"  fetched {i+len(chunk)}/{len(symbols)}", flush=True)
    return out

def to_days(bars):
    days = defaultdict(list)
    for b in bars:
        ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(CT)
        if ts.hour < 8 or (ts.hour == 8 and ts.minute < 30) or ts.hour >= 15: continue
        days[ts.date()].append({"ts": ts, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
    return days

def backtest(data, ret_bars, ret_min, vol_mult, lookback, target, stop, tstop_min, cost_bps,
             t_lo=8.6, t_hi=14.5, need_extreme=True, cooldown_min=30):
    trades = []
    for sym, days in data.items():
        for d, bars in days.items():
            last_entry = None
            for i in range(lookback + ret_bars, len(bars) - 1):
                b = bars[i]
                hr = b["ts"].hour + b["ts"].minute / 60
                if hr < t_lo or hr > t_hi: continue
                if last_entry is not None and (b["ts"] - last_entry).total_seconds() < cooldown_min * 60: continue
                prev = bars[i - ret_bars]
                r = b["c"] / prev["c"] - 1
                if abs(r) < ret_min: continue
                vols = [x["v"] for x in bars[i - lookback:i]]
                med = statistics.median(vols) if vols else 0
                if med <= 0 or b["v"] < vol_mult * med: continue
                m = 1 if r > 0 else -1
                if need_extreme:
                    win = bars[max(0, i - 30):i]
                    if m > 0 and b["c"] < max(x["h"] for x in win): continue
                    if m < 0 and b["c"] > min(x["l"] for x in win): continue
                entry = bars[i + 1]["o"]
                tgt = entry * (1 + m * target); stp = entry * (1 - m * stop)
                outcome, ex, mins = "time", None, None
                for j in range(i + 1, len(bars)):
                    x = bars[j]
                    if (x["ts"] - bars[i + 1]["ts"]).total_seconds() / 60 > tstop_min:
                        outcome, ex = "time", x["o"]; break
                    hit_s = x["l"] <= stp if m > 0 else x["h"] >= stp
                    hit_t = x["h"] >= tgt if m > 0 else x["l"] <= tgt
                    if hit_s: outcome, ex = "stop", stp; break
                    if hit_t: outcome, ex = "target", tgt; break
                else:
                    ex = bars[-1]["c"]
                if ex is None: ex = bars[-1]["c"]
                pnl = m * (ex / entry - 1) - cost_bps / 1e4
                trades.append({"sym": sym, "d": str(d), "t": b["ts"].strftime("%H:%M"), "dir": m, "r": r, "vm": b["v"] / med, "pnl": pnl, "out": outcome})
                last_entry = b["ts"]
    return trades

def summarize(name, tr):
    if not tr: print(f"{name:60s} no trades"); return
    n = len(tr); pn = [t["pnl"] for t in tr]
    days = len({t["d"] for t in tr})
    tg = sum(1 for t in tr if t["out"] == "target"); st = sum(1 for t in tr if t["out"] == "stop")
    print(f"{name:60s} n={n:5d} ({n/max(days,1):4.1f}/day) EV={statistics.mean(pn)*100:+.3f}% med={statistics.median(pn)*100:+.3f}% target={100*tg/n:3.0f}% stop={100*st/n:3.0f}% sumpnl={sum(pn)*100:+.1f}%")

async def main():
    syms = [s.strip() for s in open(f"{S}/universe.txt") if s.strip()]
    cache = f"{S}/burst_bars.json"
    if os.path.exists(cache):
        raw = json.load(open(cache))
    else:
        raw = await fetch_all(syms)
        json.dump(raw, open(cache, "w"))
    data = {s: to_days(b) for s, b in raw.items() if b}
    nb = sum(len(v) for v in raw.values())
    print(f"universe {len(data)} symbols, {nb} bars, sessions ~{max(len(v) for v in data.values())}")
    grid = [
        ("2min ret>=0.6%, vol>=4x, new 30m extreme, tgt1 stp0.5 t15, cost 10bps", dict(ret_bars=2, ret_min=.006, vol_mult=4, lookback=30, target=.01, stop=.005, tstop_min=15, cost_bps=10)),
        ("same, stop 0.7", dict(ret_bars=2, ret_min=.006, vol_mult=4, lookback=30, target=.01, stop=.007, tstop_min=15, cost_bps=10)),
        ("same, stop 1.0", dict(ret_bars=2, ret_min=.006, vol_mult=4, lookback=30, target=.01, stop=.010, tstop_min=15, cost_bps=10)),
        ("same, t30", dict(ret_bars=2, ret_min=.006, vol_mult=4, lookback=30, target=.01, stop=.007, tstop_min=30, cost_bps=10)),
        ("1min ret>=0.5%, vol>=5x, extreme, tgt1 stp0.7 t15", dict(ret_bars=1, ret_min=.005, vol_mult=5, lookback=30, target=.01, stop=.007, tstop_min=15, cost_bps=10)),
        ("3min ret>=0.8%, vol>=3x, extreme, tgt1 stp0.7 t15", dict(ret_bars=3, ret_min=.008, vol_mult=3, lookback=30, target=.01, stop=.007, tstop_min=15, cost_bps=10)),
        ("2min ret>=0.6%, vol>=4x, NO extreme req", dict(ret_bars=2, ret_min=.006, vol_mult=4, lookback=30, target=.01, stop=.007, tstop_min=15, cost_bps=10, need_extreme=False)),
        ("2min ret>=1.0%, vol>=6x, extreme (rarer, bigger)", dict(ret_bars=2, ret_min=.010, vol_mult=6, lookback=30, target=.01, stop=.007, tstop_min=15, cost_bps=10)),
        ("2min ret>=0.6%, vol>=4x, first hour only (08:36-09:30)", dict(ret_bars=2, ret_min=.006, vol_mult=4, lookback=5, target=.01, stop=.007, tstop_min=15, cost_bps=10, t_lo=8.6, t_hi=9.5)),
        ("2min ret>=0.6%, vol>=4x, after 09:30 only", dict(ret_bars=2, ret_min=.006, vol_mult=4, lookback=30, target=.01, stop=.007, tstop_min=15, cost_bps=10, t_lo=9.5, t_hi=14.5)),
        ("target 0.5%, stop 0.35%, t10 (smaller snipe)", dict(ret_bars=2, ret_min=.006, vol_mult=4, lookback=30, target=.005, stop=.0035, tstop_min=10, cost_bps=10)),
        ("FADE: 2min ret>=0.6% vol>=4x, trade AGAINST it, tgt1 stp0.7 t15", dict(ret_bars=2, ret_min=.006, vol_mult=4, lookback=30, target=.01, stop=.007, tstop_min=15, cost_bps=10, need_extreme=False)),
    ]
    results = {}
    for name, kw in grid:
        tr = backtest(data, **kw)
        if name.startswith("FADE"):
            for t in tr: t["pnl"] = -(t["pnl"] + 10 / 1e4) - 10 / 1e4  # flip direction, keep cost
        summarize(name, tr)
        results[name] = tr
    # direction and day split for the main config
    main_tr = results[grid[1][0]]
    for label, sel in (("  longs", lambda t: t["dir"] > 0), ("  shorts", lambda t: t["dir"] < 0)):
        summarize(label, [t for t in main_tr if sel(t)])
    byday = defaultdict(list)
    for t in main_tr: byday[t["d"]].append(t["pnl"])
    print("  per-day EV (main config):", " ".join(f"{d[5:]}:{statistics.mean(v)*100:+.2f}" for d, v in sorted(byday.items())))
    json.dump({k: v for k, v in results.items()}, open(f"{S}/burst_results.json", "w"))

asyncio.run(main())
