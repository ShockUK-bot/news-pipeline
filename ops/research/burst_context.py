import os
"""Split burst trades by context: news-anchored (an A1 escalation for the ticker in
the 15 min before or 2 min after the burst bar), scanner-known (the scanner had a
row for the ticker that day before the burst), or bare. Reuses the cached bars."""
import json, statistics, sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
sys.argv = [sys.argv[0]]
S = os.environ.get("RESEARCH_DIR", "/tmp/research")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import burst_backtest as B
CT = ZoneInfo("America/Chicago")

esc = defaultdict(list)
for line in open(f"{S}/escalations.txt"):
    t, ts, d = line.strip().split("|")
    esc[t].append((datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=CT), d))
scan = defaultdict(list)
for line in open(f"{S}/scanner_rows.txt"):
    t, ts, st = line.strip().split("|")
    scan[t].append(datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=CT))

raw = json.load(open(f"{S}/burst_bars.json"))
data = {s: B.to_days(b) for s, b in raw.items() if b}

def context(tr):
    t0 = datetime.strptime(tr["d"] + " " + tr["t"], "%Y-%m-%d %H:%M").replace(tzinfo=CT)
    news = None
    for ts, d in esc.get(tr["sym"], []):
        if -15 * 60 <= (t0 - ts).total_seconds() <= 2 * 60:
            news = d
    sc = any(ts.date() == t0.date() and ts <= t0 for ts in scan.get(tr["sym"], []))
    return news, sc

def summ(name, tr):
    if len(tr) < 5: print(f"{name:58s} n={len(tr):4d} (too few)"); return
    pn = [t["pnl"] for t in tr]; n = len(tr)
    tg = sum(1 for t in tr if t["out"] == "target"); st = sum(1 for t in tr if t["out"] == "stop")
    print(f"{name:58s} n={n:4d} EV={statistics.mean(pn)*100:+.3f}% med={statistics.median(pn)*100:+.3f}% target={100*tg/n:3.0f}% stop={100*st/n:3.0f}%")

for cfg_name, kw in (("follow tgt1 stp0.7 t15", dict(target=.01, stop=.007, tstop_min=15)),
                     ("follow tgt1 stp1.0 t30", dict(target=.01, stop=.010, tstop_min=30)),
                     ("follow tgt2 stp1.0 t60", dict(target=.02, stop=.010, tstop_min=60))):
    tr = B.backtest(data, ret_bars=2, ret_min=.006, vol_mult=4, lookback=30, cost_bps=10, need_extreme=False, **kw)
    groups = defaultdict(list)
    for t in tr:
        news, sc = context(t)
        key = "news-anchored" if news else ("scanner-known" if sc else "bare")
        groups[key].append(t)
        if news:
            agree = (news == "up" and t["dir"] > 0) or (news == "down" and t["dir"] < 0)
            groups["news-anchored & direction agrees" if agree else "news-anchored & direction disagrees"].append(t)
    print(f"\n== {cfg_name}")
    summ("all", tr)
    for k in ("bare", "scanner-known", "news-anchored", "news-anchored & direction agrees", "news-anchored & direction disagrees"):
        summ("  " + k, groups.get(k, []))
    # fade the bare ones
    bare = groups.get("bare", [])
    fade = [{**t, "pnl": -(t["pnl"] + 10 / 1e4) - 10 / 1e4} for t in bare]
    summ("  FADE bare (against the burst)", fade)
