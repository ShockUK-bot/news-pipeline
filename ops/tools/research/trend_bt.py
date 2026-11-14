"""Back test of the EXACT live trend day trigger (config/trend_day.yaml):
from 10:00 ET, any of QQQ / SPY at or below -2% vs previous close AND below its
own 09:45 ET price; first hit sets the flag for the session. READ ONLY: Alpaca
bars only, no journal writes. Stage 1: index. Stage 2: liquid single names on
the trigger days (what the mode actually shorts)."""
import asyncio, json, os, sys, statistics as st
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import httpx

sys.path.insert(0, "/opt/pipeline/src")
ET = ZoneInfo("America/New_York")
BASE = "https://data.alpaca.markets"
H = {"APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}
FEED = os.environ.get("ALPACA_FEED", "sip")
OUT = sys.argv[1]
START, END = "2024-11-01", "2026-11-13"
MIN_DROP = 0.02


async def bars(client, symbols, start, end, tf="5Min"):
    """Multi symbol bars with pagination, one gentle request at a time."""
    out, token = defaultdict(list), None
    while True:
        p = {"symbols": ",".join(symbols), "timeframe": tf, "start": start, "end": end,
             "limit": 10000, "adjustment": "split", "feed": FEED}
        if token:
            p["page_token"] = token
        r = await client.get(f"{BASE}/v2/stocks/bars", params=p)
        r.raise_for_status()
        d = r.json()
        for s, bs in (d.get("bars") or {}).items():
            for b in bs:
                t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
                hm = t.strftime("%H:%M")
                if "09:30" <= hm < "16:00":
                    out[s].append((t, b["o"], b["h"], b["l"], b["c"]))
        token = d.get("next_page_token")
        await asyncio.sleep(0.5)
        if not token:
            return out


def sessions(bs):
    days = defaultdict(list)
    for b in bs:
        days[b[0].date()].append(b)
    return dict(sorted(days.items()))


def trigger(day_bars, prev_close):
    """First 5 minute bar CLOSE from 10:00 ET that meets the live rule.
    Returns (time, price) or None. The 09:45 price is the open of the 09:45 bar."""
    p0945 = next((b[1] for b in day_bars if b[0].strftime("%H:%M") == "09:45"), None)
    if not p0945 or not prev_close:
        return None
    for b in day_bars:
        end = b[0] + timedelta(minutes=5)
        hm = end.strftime("%H:%M")
        if hm < "10:00" or hm > "15:15":
            continue
        last = b[4]
        if last / prev_close - 1 <= -MIN_DROP and last < p0945:
            return end, last
    return None


def flat_px(day_bars):
    b = [x for x in day_bars if x[0].strftime("%H:%M") < "15:50"]
    return b[-1][4] if b else None


async def main():
    res = {"index": [], "names": []}
    async with httpx.AsyncClient(timeout=30.0, headers=H) as client:
        idx = await bars(client, ["QQQ", "SPY"], START, END)
        trig_days = {}
        nsess = 0
        for sym in ("QQQ", "SPY"):
            ses = sessions(idx[sym]); nsess = max(nsess, len(ses))
            prev = None
            for d, bs in ses.items():
                if prev is not None:
                    t = trigger(bs, prev)
                    if t:
                        tt, px = t
                        after = [b for b in bs if b[0] >= tt and b[0].strftime("%H:%M") < "15:50"]
                        fl = flat_px(bs)
                        if after and fl:
                            ret = fl / px - 1                      # index move after the trigger
                            bounce = max(b[2] for b in after) / px - 1
                            res["index"].append({"sym": sym, "day": str(d), "t": tt.strftime("%H:%M"),
                                                 "at_trigger": round(px / prev - 1, 4), "to_close": round(ret, 4),
                                                 "max_bounce": round(bounce, 4), "day_close": round(bs[-1][4] / prev - 1, 4)})
                            if str(d) not in trig_days or tt < trig_days[str(d)]:
                                trig_days[str(d)] = tt
                prev = bs[-1][4]
        res["sessions"] = nsess
        res["trigger_days"] = {k: v.strftime("%H:%M") for k, v in sorted(trig_days.items())}

        # ---- stage 2: liquid single names on the trigger days
        from common.db import get_pool, close_pool
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute("""
                SELECT ticker, max((metrics->>'adv20_dollars')::numeric) adv FROM journal.scanner_candidates
                WHERE metrics ? 'adv20_dollars'
                  AND ticker NOT IN (SELECT DISTINCT ticker FROM journal.scanner_candidates
                                     WHERE reject_reason IN ('ETF_EXCLUDED','INSTRUMENT_SHAPE'))
                GROUP BY 1 HAVING max((metrics->>'adv20_dollars')::numeric) >= 100000000
                ORDER BY 2 DESC LIMIT 400""")
            uni = [r[0] for r in await cur.fetchall()]
        await close_pool()
        res["universe"] = len(uni)
        for day, tt in sorted(trig_days.items()):
            d = datetime.strptime(day, "%Y-%m-%d").date()
            start = (d - timedelta(days=5)).isoformat()
            end = (d + timedelta(days=1)).isoformat()
            for i in range(0, len(uni), 100):
                data = await bars(client, uni[i:i + 100], start, end)
                for s, bs in data.items():
                    ses = sessions(bs)
                    ds = list(ses)
                    if d not in ses or ds.index(d) == 0:
                        continue
                    prev = ses[ds[ds.index(d) - 1]][-1][4]
                    today = ses[d]
                    at = [b for b in today if b[0] + timedelta(minutes=5) <= tt]
                    after = [b for b in today if b[0] >= tt and b[0].strftime("%H:%M") < "15:50"]
                    if not at or not after or not prev:
                        continue
                    px = at[-1][4]
                    fl = after[-1][4]
                    res["names"].append({"day": day, "sym": s, "move_at_trigger": round(px / prev - 1, 4),
                                         "short_to_close": round(1 - fl / px, 4),
                                         "short_mae": round(max(b[2] for b in after) / px - 1, 4)})
            json.dump(res, open(OUT, "w"))
    json.dump(res, open(OUT, "w"))

    # ---- report
    print(f"sessions {res['sessions']}, trigger days {len(res['trigger_days'])}, universe {res['universe']}")
    for r in sorted(res["index"], key=lambda x: (x["day"], x["sym"])):
        print(f"  {r['day']} {r['sym']} trig {r['t']} at {r['at_trigger']*100:+.1f}%  then to 15:50 {r['to_close']*100:+.2f}%  "
              f"max bounce {r['max_bounce']*100:+.2f}%  day close {r['day_close']*100:+.1f}%")
    for sym in ("QQQ", "SPY"):
        x = [r["to_close"] for r in res["index"] if r["sym"] == sym]
        if x:
            print(f"{sym}: n={len(x)} mean after trigger {st.mean(x)*100:+.2f}% median {st.median(x)*100:+.2f}% kept falling {sum(v < 0 for v in x)}/{len(x)}")
    print("\nsingle names, short from the trigger to 15:50, by day:")
    byday = defaultdict(list)
    for n in res["names"]:
        byday[n["day"]].append(n)
    tot_all, tot_dn = [], []
    for day, ns in sorted(byday.items()):
        a = [n["short_to_close"] for n in ns]
        dn = [n for n in ns if n["move_at_trigger"] <= -0.04]
        b = [n["short_to_close"] for n in dn]
        tot_all.append(st.mean(a));
        if b: tot_dn.append((day, st.mean(b), len(b)))
        print(f"  {day} trig {res['trigger_days'][day]} all n={len(a)} mean {st.mean(a)*100:+.2f}% win {100*sum(v > 0 for v in a)/len(a):.0f}% | "
              f"down 4%+ movers n={len(b)} " + (f"mean {st.mean(b)*100:+.2f}% win {100*sum(v > 0 for v in b)/len(b):.0f}% med MAE {st.median(n['short_mae'] for n in dn)*100:.1f}%" if b else ""))
    if tot_all:
        print(f"\nday level (each day one vote): all names mean of daily means {st.mean(tot_all)*100:+.2f}%, days positive {sum(v > 0 for v in tot_all)}/{len(tot_all)}")
    if tot_dn:
        v = [x[1] for x in tot_dn]
        print(f"down 4%+ movers: mean of daily means {st.mean(v)*100:+.2f}%, days positive {sum(x > 0 for x in v)}/{len(v)}")

asyncio.run(main())
