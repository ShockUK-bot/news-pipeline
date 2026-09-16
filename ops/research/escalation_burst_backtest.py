"""News-anchored snipe test. For every RTH A1 escalation with a direction hint
(last 30 days), fetch that ticker-day's 1-minute bars and test:
  A. enter next bar after the escalation minute in the hinted direction
  B. enter only if a burst confirms within 5 min (2-min ret >= 0.4% in the
     hinted direction on >= 3x median volume), next bar after confirmation
Bracket target/stop, time stop, 10 bps round-trip cost."""
import asyncio, os, json, os, statistics, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import httpx
CT = ZoneInfo("America/Chicago")
S = os.environ.get("RESEARCH_DIR", "/tmp/research")
H = {"APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}

esc = []
for line in open(f"{S}/escalations.txt"):
    t, ts, d = line.strip().split("|")
    if d not in ("up", "down"): continue
    ts = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=CT)
    hr = ts.hour + ts.minute / 60
    if hr < 8.6 or hr > 14.5: continue
    esc.append((t, ts, 1 if d == "up" else -1))
# one escalation per ticker-day-minute
esc = sorted({(t, ts, m) for t, ts, m in esc}, key=lambda x: x[1])
by_day = defaultdict(set)
for t, ts, m in esc: by_day[ts.date()].add(t)
print(f"{len(esc)} RTH escalations with a hint over {len(by_day)} days, {sum(len(v) for v in by_day.values())} ticker-days", flush=True)

async def fetch():
    cache = f"{S}/escal_bars.json"
    if os.path.exists(cache): return json.load(open(cache))
    out = {}
    async with httpx.AsyncClient(timeout=60, headers=H) as c:
        for d, syms in sorted(by_day.items()):
            syms = sorted(syms)
            s = datetime(d.year, d.month, d.day, 8, 30, tzinfo=CT).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            e = datetime(d.year, d.month, d.day, 15, 0, tzinfo=CT).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for i in range(0, len(syms), 40):
                chunk = syms[i:i+40]; token = None
                while True:
                    p = {"symbols": ",".join(chunk), "timeframe": "1Min", "start": s, "end": e, "limit": 10000, "feed": "sip", "adjustment": "raw"}
                    if token: p["page_token"] = token
                    r = await c.get("https://data.alpaca.markets/v2/stocks/bars", params=p)
                    if r.status_code != 200: break
                    dd = r.json()
                    for sym, bars in (dd.get("bars") or {}).items():
                        out.setdefault(f"{sym}:{d}", []).extend(bars)
                    token = dd.get("next_page_token")
                    if not token: break
            print(f"  {d}: {len(syms)} symbols", flush=True)
    json.dump(out, open(cache, "w"))
    return out

def bars_of(raw, t, d):
    out = []
    for b in raw.get(f"{t}:{d}", []):
        ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(CT)
        out.append({"ts": ts, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
    return out

def run_bracket(bars, i_entry, m, target, stop, tstop, cost_bps):
    entry = bars[i_entry]["o"]; tgt = entry * (1 + m * target); stp = entry * (1 - m * stop)
    ex, out = bars[-1]["c"], "eod"
    for j in range(i_entry, len(bars)):
        x = bars[j]
        if (x["ts"] - bars[i_entry]["ts"]).total_seconds() / 60 > tstop: ex, out = x["o"], "time"; break
        hit_s = x["l"] <= stp if m > 0 else x["h"] >= stp
        hit_t = x["h"] >= tgt if m > 0 else x["l"] <= tgt
        if hit_s: ex, out = stp, "stop"; break
        if hit_t: ex, out = tgt, "target"; break
    return m * (ex / entry - 1) - cost_bps / 1e4, out

def summ(name, tr):
    if len(tr) < 8: print(f"{name:52s} n={len(tr):4d} (too few)"); return
    pn = [p for p, _ in tr]; n = len(tr)
    tg = sum(1 for _, o in tr if o == "target"); st = sum(1 for _, o in tr if o == "stop")
    print(f"{name:52s} n={n:4d} EV={statistics.mean(pn)*100:+.3f}% med={statistics.median(pn)*100:+.3f}% target={100*tg/n:3.0f}% stop={100*st/n:3.0f}%")

async def main():
    raw = await fetch()
    for target, stop, tstop in ((.01, .007, 15), (.01, .01, 30), (.02, .01, 60), (.005, .005, 15)):
        A, B_, A_up, A_dn, B_up, B_dn, Bhi = [], [], [], [], [], [], []
        for t, ts, m in esc:
            bars = bars_of(raw, t, ts.date())
            if len(bars) < 40: continue
            idx = next((k for k, b in enumerate(bars) if b["ts"] >= ts), None)
            if idx is None or idx + 2 >= len(bars) or idx < 31: continue
            r = run_bracket(bars, idx + 1, m, target, stop, tstop, 10)
            A.append(r); (A_up if m > 0 else A_dn).append(r)
            # B: burst confirmation within 5 bars after the escalation bar
            for k in range(idx + 2, min(idx + 7, len(bars) - 1)):
                ret = bars[k]["c"] / bars[k - 2]["c"] - 1
                vols = [x["v"] for x in bars[k - 30:k]]; med = statistics.median(vols) if vols else 0
                if med > 0 and m * ret >= .004 and bars[k]["v"] >= 3 * med:
                    rb = run_bracket(bars, k + 1, m, target, stop, tstop, 10)
                    B_.append(rb); (B_up if m > 0 else B_dn).append(rb)
                    if m * ret >= .008: Bhi.append(rb)
                    break
        print(f"\n== target {target*100:.1f}% stop {stop*100:.1f}% t{tstop}")
        summ("A enter on escalation (hinted dir)", A); summ("  A longs", A_up); summ("  A shorts", A_dn)
        summ("B enter on burst confirm <=5min", B_); summ("  B longs", B_up); summ("  B shorts", B_dn); summ("  B strong burst (>=0.8%)", Bhi)

asyncio.run(main())
