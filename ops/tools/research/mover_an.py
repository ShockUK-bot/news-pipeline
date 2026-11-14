import json, sys, statistics as st
from collections import defaultdict
E = [json.loads(l) for l in open(sys.argv[1])]
COST = 0.001
for e in E:
    up = e["move"] > 0
    c = COST / e["runit"]
    fo, fa = ("L", "S") if up else ("S", "L")
    e["fo_lad"], e["fa_lad"] = e[fo + "_lad"] - c, e[fa + "_lad"] - c
    e["fo_stp"], e["fa_stp"] = e[fo + "_stp"] - c, e[fa + "_stp"] - c
    h = e["hold_long_pct"] * (1 if up else -1)
    e["fo_hold"], e["fa_hold"] = (h - COST) * 100, (-h - COST) * 100
    e["y"] = 1 if e["day"] < "2025-11-13" else 2
    q = e["qqq"]
    e["reg"] = "?" if q is None else ("idx down 1%+" if q <= -0.01 else "idx up 1%+" if q >= 0.01 else "idx flat")
    e["dir"] = "up" if up else "down"
    e["tb"] = "1 by 10:00" if e["hm"] <= 600 else "2 10:00-11:30" if e["hm"] <= 690 else "3 11:30-14:00" if e["hm"] <= 840 else "4 after 14:00"
    m = abs(e["move"]); e["sz"] = "4-6%" if m < 0.06 else "6-10%" if m < 0.10 else "10%+"
    e["wi"] = "?" if q is None or abs(q) < 0.005 else ("with index" if (q > 0) == up else "against index")

def stat(rows, k):
    if not rows: return None
    d = defaultdict(list)
    for r in rows: d[r["day"]].append(r[k])
    dm = [st.mean(v) for v in d.values()]
    t = st.mean(dm) / (st.pstdev(dm) / len(dm) ** 0.5) if len(dm) > 2 and st.pstdev(dm) > 0 else 0
    x = [r[k] for r in rows]
    return len(x), len(dm), st.mean(x), 100 * sum(v > 0 for v in x) / len(x), t

def line(label, rows, k):
    a = stat(rows, k); y1 = stat([r for r in rows if r["y"] == 1], k); y2 = stat([r for r in rows if r["y"] == 2], k)
    if not a or a[0] < 30: return
    f = lambda s: "     ." if not s else f"{s[2]:+6.3f}"
    print(f"  {label:34s} n={a[0]:6d} days={a[1]:4d} mean {a[2]:+6.3f} win {a[3]:3.0f}% t={a[4]:+5.1f} | y1 {f(y1)} y2 {f(y2)}")

for trig in ("A", "B"):
    T = [e for e in E if e["trig"] == trig]
    print(f"\n===== trigger {trig}: {len(T)} events, {len({e['day'] for e in T})} days  (R net of 10bp; hold in % net)")
    for k in ("fo_lad", "fa_lad", "fo_stp", "fa_stp", "fo_hold", "fa_hold"):
        line(f"ALL {k}", T, k)
    for split in ("dir", "reg", "tb", "sz", "wi"):
        for k in ("fo_lad", "fa_stp", "fa_hold"):
            print(f" -- by {split}: {k}")
            for v in sorted({e[split] for e in T}):
                line(v, [e for e in T if e[split] == v], k)
    print(" -- dir x reg: fa_stp / fo_lad")
    for d in ("up", "down"):
        for v in sorted({e["reg"] for e in T}):
            rows = [e for e in T if e["dir"] == d and e["reg"] == v]
            line(f"{d} mover, {v} fa_stp", rows, "fa_stp"); line(f"{d} mover, {v} fo_lad", rows, "fo_lad")
