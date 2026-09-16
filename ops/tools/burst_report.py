#!/usr/bin/env python
"""burst_report — what C12 saw today and how the rules are scoring (v0.15.0).

Usage (from /opt/pipeline, env sourced):
    .venv/bin/python ops/tools/burst_report.py            # today
    .venv/bin/python ops/tools/burst_report.py --days 5   # last 5 sessions

Read only. Prints the day's events (newest first) and, per rule and
direction, how often the 1% target came before the 0.7% stop within 30 min,
the average 30-minute return in the rule's direction, and the go-live bar
from the design memo (200+ events, EV >= +0.15% after cost, <= 40% of
sessions negative)."""
import argparse
import asyncio
import sys

sys.path.insert(0, "src")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--cost-bps", type=float, default=10.0)
    args = ap.parse_args()
    from common.db import get_pool, close_pool
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT ts AT TIME ZONE 'America/Chicago', symbol, rule, direction, price,
                      round(ret_60s*100,2), round(ret_120s*100,2), round(vol_mult,1), spread_bps,
                      news_anchored, scanner_known, complete, first_hit, first_hit_min,
                      round(max_fav_pct*100,2), round(max_adv_pct*100,2),
                      round((CASE WHEN direction='up' THEN p_30m/price-1 ELSE 1-p_30m/price END)*100,2)
               FROM journal.burst_events
               WHERE ts >= (current_date - (%s - 1)) AND rule='momentum'
               ORDER BY ts DESC LIMIT 60""", (args.days,))
        rows = await cur.fetchall()
        cur = await conn.execute(
            """SELECT rule, direction, count(*) AS n, count(*) FILTER (WHERE complete) AS done,
                      round(100.0*count(*) FILTER (WHERE first_hit='target')/NULLIF(count(*) FILTER (WHERE complete),0)) AS target_pct,
                      round(100.0*count(*) FILTER (WHERE first_hit='stop')/NULLIF(count(*) FILTER (WHERE complete),0)) AS stop_pct,
                      round(avg(CASE WHEN direction='up' THEN p_30m/price-1 ELSE 1-p_30m/price END) FILTER (WHERE complete)*100, 3) AS avg_30m_pct,
                      round(avg(spread_bps),1) AS avg_spread_bps,
                      count(DISTINCT ts::date) AS sessions,
                      count(DISTINCT ts::date) FILTER (WHERE complete) AS scored_sessions
               FROM journal.burst_events
               WHERE ts >= (current_date - (%s - 1))
               GROUP BY 1,2 ORDER BY 1,2""", (args.days,))
        score = await cur.fetchall()
        cur = await conn.execute(
            """SELECT ts::date AS d, rule, direction,
                      avg(CASE WHEN direction='up' THEN p_30m/price-1 ELSE 1-p_30m/price END) AS ev
               FROM journal.burst_events WHERE complete AND ts >= (current_date - (%s - 1))
               GROUP BY 1,2,3""", (args.days,))
        per_day = await cur.fetchall()
    await close_pool()

    print(f"C12 burst events, last {args.days} session(s): {len(rows)} momentum detections shown (newest first)")
    print(f"{'time':8s} {'sym':6s} {'dir':4s} {'price':>9s} {'r60':>6s} {'r120':>6s} {'vol':>5s} {'spr':>6s} news scan  outcome")
    for r in rows:
        ts, sym, rule, d, px, r60, r120, vm, spr, news, sc, done, hit, hmin, fav, adv, r30 = r
        out = (f"{hit} @{hmin}m  fav {fav}% adv {adv}%  30m {r30:+}%" if done else "pending")
        print(f"{ts.strftime('%H:%M:%S'):8s} {sym:6s} {d:4s} {float(px):9.2f} {str(r60):>6s} {str(r120):>6s} {str(vm):>5s} {str(spr):>6s} {'y' if news else '.':^4s} {'y' if sc else '.':^4s} {out}")
    print("\nScoreboard (direction-adjusted; 1% target vs 0.7% stop within 30 min)")
    print(f"{'rule':14s} {'dir':4s} {'n':>5s} {'done':>5s} {'target%':>8s} {'stop%':>6s} {'avg30m%':>8s} {'EV after cost':>14s} {'spread':>7s} {'neg sessions':>13s}")
    neg = {}
    for d, rule, direction, ev in per_day:
        neg.setdefault((rule, direction), [0, 0])
        neg[(rule, direction)][1] += 1
        if ev is not None and ev < 0:
            neg[(rule, direction)][0] += 1
    for rule, direction, n, done, tp, sp, avg30, spr, sess, ssess in score:
        ev_cost = (float(avg30) - args.cost_bps / 100) if avg30 is not None else None
        nneg, ntot = neg.get((rule, direction), (0, 0))
        bar = "GO" if (done or 0) >= 200 and ev_cost is not None and ev_cost >= 0.15 and ntot and nneg / ntot <= 0.4 else "no"
        print(f"{rule:14s} {direction:4s} {n:5d} {done:5d} {str(tp):>8s} {str(sp):>6s} {str(avg30):>8s} {(f'{ev_cost:+.3f}%' if ev_cost is not None else '-'):>14s} {str(spr):>7s} {nneg}/{ntot:<11d} {bar}")

    # v0.15.3: bracket sweep (rows scored since the multi-bracket change)
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT rule, direction, b.key AS bracket,
                      count(*) AS n,
                      count(*) FILTER (WHERE b.value->>0 = 'target') AS tgt,
                      count(*) FILTER (WHERE b.value->>0 = 'stop') AS stp
               FROM journal.burst_events e, jsonb_each(e.detail->'brackets') b
               WHERE e.complete AND e.ts >= (current_date - (%s - 1))
               GROUP BY 1,2,3 ORDER BY 1,2,3""", (args.days,))
        rows = await cur.fetchall()
    await close_pool()
    if rows:
        print("\nBracket sweep (target/stop in %, first hit within 30 min; EV assumes exits at the levels, cost applied)")
        print(f"{'rule':14s} {'dir':4s} {'bracket':12s} {'n':>5s} {'target%':>8s} {'stop%':>6s} {'EV/trade':>9s}")
        for rule, direction, bracket, n, tgt, stp in rows:
            t = float(bracket.split("_")[0][1:]); s = float(bracket.split("_")[1][1:])
            ev = (tgt * t - stp * s) / n - args.cost_bps / 100
            print(f"{rule:14s} {direction:4s} {bracket:12s} {n:5d} {100*tgt/n:7.0f}% {100*stp/n:5.0f}% {ev:+8.3f}%")


if __name__ == "__main__":
    asyncio.run(main())
