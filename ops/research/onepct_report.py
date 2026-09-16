import os
import json
from collections import defaultdict
R = json.load(open(os.environ.get("RESEARCH_DIR", "/tmp/research") + "/onepct.json"))

def bucket_hr(h):
    return "a 08:30-09:00" if h < 9 else "b 09:00-10:00" if h < 10 else "c 10:00-12:00" if h < 12 else "d 12:00-14:00" if h < 14 else "e 14:00+"
def bucket_mv(m):
    m = abs(m or 0)
    return "a <3%" if m < .03 else "b 3-5%" if m < .05 else "c 5-8%" if m < .08 else "d 8-12%" if m < .12 else "e 12%+"
def bucket_rv(r):
    if r is None: return "n/a"
    return "a <2x" if r < 2 else "b 2-4x" if r < 4 else "c 4-8x" if r < 8 else "d 8x+"
def bucket_fresh(rec):
    m = rec["m_hod"] if rec["dir"] > 0 else rec["m_lod"]
    if m is None: return "n/a"
    return "a 0-2m" if m <= 2 else "b 3-10m" if m <= 10 else "c 11-30m" if m <= 30 else "d 30m+"

def table(title, keyfn, metric="1v1_30", min_n=15, rows=R):
    g = defaultdict(list)
    for r in rows:
        g[keyfn(r)].append(r)
    print(f"\n== {title}  [{metric}]  (n, fav%, adv%, none%, med_min_to_fav)")
    for k in sorted(g):
        rs = g[k]
        if len(rs) < min_n: continue
        o = [r[metric][0] for r in rs]
        fav = sum(1 for x in o if x == "fav"); adv = sum(1 for x in o if x in ("adv", "both")); none = sum(1 for x in o if x == "none")
        mins = sorted(r[metric][1] for r in rs if r[metric][0] == "fav")
        med = mins[len(mins)//2] if mins else None
        print(f"  {str(k):28s} n={len(rs):4d} fav={100*fav/len(rs):3.0f}% adv={100*adv/len(rs):3.0f}% none={100*none/len(rs):3.0f}%  {('t_fav~%dm' % med) if med is not None else ''}")

print(f"records: {len(R)}  scanner={sum(1 for r in R if r['src']=='scanner')}  gate={sum(1 for r in R if r['src']=='gate')}")
for metric in ("1v1_30", "1v1_60", "1v05_60", "05v05_30", "1v2_60"):
    table("by source x direction", lambda r: f"{r['src']}/{'long' if r['dir']>0 else 'short'}", metric, 20)
S = [r for r in R if r["src"] == "scanner"]
G = [r for r in R if r["src"] == "gate"]
table("scanner: by time of day", lambda r: bucket_hr(r["hr"]) + ("/L" if r["dir"] > 0 else "/S"), "1v1_30", 15, S)
table("scanner: by move at detection", lambda r: bucket_mv(r["move"]) + ("/L" if r["dir"] > 0 else "/S"), "1v1_30", 15, S)
table("scanner: by rel volume", lambda r: bucket_rv(r["relvol"]) + ("/L" if r["dir"] > 0 else "/S"), "1v1_30", 15, S)
table("scanner: by freshness (min since extreme)", lambda r: bucket_fresh(r) + ("/L" if r["dir"] > 0 else "/S"), "1v1_30", 15, S)
table("scanner: by status", lambda r: r["status"] + ("/L" if r["dir"] > 0 else "/S"), "1v1_30", 10, S)
table("scanner: fresh (<=2m) x relvol", lambda r: (bucket_fresh(r), bucket_rv(r["relvol"]), "L" if r["dir"] > 0 else "S"), "1v1_30", 12, S)
table("scanner: fresh (<=2m) x time", lambda r: (bucket_fresh(r), bucket_hr(r["hr"]), "L" if r["dir"] > 0 else "S"), "1v1_30", 12, S)
table("gate: by time x direction", lambda r: bucket_hr(r["hr"]) + ("/L" if r["dir"] > 0 else "/S"), "1v1_30", 15, G)
table("gate: by rule/reason x direction", lambda r: r["status"] + ("/L" if r["dir"] > 0 else "/S"), "1v1_30", 15, G)
table("gate: by move x direction", lambda r: bucket_mv(r["move"]) + ("/L" if r["dir"] > 0 else "/S"), "1v1_30", 15, G)
