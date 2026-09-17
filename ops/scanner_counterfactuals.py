"""Nightly scanner counterfactuals (v0.16.0). For every CLOSED origin=scanner
position without rows in journal.scanner_counterfactuals, replay the scalp_v1
ladder on Alpaca 1-minute bars under each variant in ops/tools/scalp_replay
(base, noscale, runner_hold, notime, stop3.0, trail2.5, trail3.0) and journal
the result; also fill journal.trade_metrics for the position (holding time,
MAE/MFE in R, realised R, exit efficiency, predicted vs realised magnitude).
Read only against the broker; writes only the two research tables. The
first run backfills every historical scanner trade.

Run: PYTHONPATH=src .venv/bin/python ops/scanner_counterfactuals.py [--report] [--days N]
Timer: scanner-counterfactual.timer, 15:05 CT (after force-flat, before A7).
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))

from scalp_replay import VARIANTS, fetch_bars, load_position, replay_variants  # noqa: E402


async def pending_ids(conn) -> list[int]:
    cur = await conn.execute(
        """SELECT p.position_id FROM journal.positions p
           WHERE p.origin='scanner' AND p.status='CLOSED' AND p.qty_initial > 0
             AND EXISTS (SELECT 1 FROM journal.exits e WHERE e.position_id=p.position_id)
             AND NOT EXISTS (SELECT 1 FROM journal.scanner_counterfactuals c
                             WHERE c.position_id=p.position_id)
           ORDER BY p.position_id""")
    return [r[0] for r in await cur.fetchall()]


async def process(conn, pid: int) -> dict | None:
    p = await load_position(pid)
    bars = await fetch_bars(p["ticker"], p["entry_ts"])
    if not bars:
        return None
    res = replay_variants(bars, p["side"], p["entry_ts"], p["entry_px"], p["qty"], p["atr"], p["mag"])
    async with conn.transaction():
        for name, r in res.items():
            await conn.execute(
                """INSERT INTO journal.scanner_counterfactuals
                   (position_id, variant, pnl, r_multiple, mfe_r, close_r, exits, bars)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (position_id, variant) DO NOTHING""",
                (pid, name, r["pnl"], r["r"], r["mfe_r"], r["close_r"],
                 json.dumps(r["exits"]), len(bars)))
        base = res["base"]
        # trade_metrics: MAE from the same bars, realised R from the journal
        m = 1 if p["side"] == "LONG" else -1
        first = p["entry_ts"].replace(second=0, microsecond=0)
        end = p["closed_ts"] or bars[-1]["ts"]
        held = [b for b in bars if first <= b["ts"] <= end]
        mae = 0.0
        for b in held:
            lose = b["low"] if p["side"] == "LONG" else b["high"]
            mae = max(mae, -m * (lose - p["entry_px"]) / p["r_unit"])
        mfe = max([0.0] + [m * ((b["high"] if p["side"] == "LONG" else b["low"]) - p["entry_px"]) / p["r_unit"] for b in held])
        realized_r = p["actual_r"] if p["actual_r"] is not None else 0.0
        eff = round(realized_r / mfe, 4) if mfe > 0 else None
        mag_real = round(max((mfe * p["r_unit"]) / p["entry_px"], 0.0), 4)
        hold_s = int((end - p["entry_ts"]).total_seconds()) if p["closed_ts"] else None
        await conn.execute(
            """INSERT INTO journal.trade_metrics
               (position_id, holding_seconds, mae_r, mfe_r, realized_r, exit_efficiency,
                magnitude_predicted, magnitude_realized, window_hit)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (position_id) DO NOTHING""",
            (pid, hold_s or 0, round(mae, 3), round(mfe, 3), round(realized_r, 3), eff,
             round(p["mag"], 4), mag_real,
             any(e[1] == "TARGET" for e in base["exits"])))
    return {"pid": pid, "ticker": p["ticker"], "actual": p["actual_pnl"],
            **{k: v["pnl"] for k, v in res.items()}}


async def report(conn, days: int | None) -> None:
    where = "" if not days else f"AND p.closed_ts >= current_date - {int(days)}"
    cur = await conn.execute(f"""
        SELECT c.variant, count(*) AS n, round(sum(c.pnl),2) AS pnl, round(sum(c.r_multiple),2) AS r,
               count(*) FILTER (WHERE c.pnl > 0) AS winners,
               round(sum(c.pnl) - (SELECT sum(b.pnl) FROM journal.scanner_counterfactuals b
                                   JOIN journal.positions q USING (position_id)
                                   WHERE b.variant='base' {where.replace('p.', 'q.')}), 2) AS vs_base
        FROM journal.scanner_counterfactuals c JOIN journal.positions p USING (position_id)
        WHERE true {where}
        GROUP BY 1 ORDER BY pnl DESC""")
    rows = await cur.fetchall()
    cur = await conn.execute(f"""
        SELECT count(*), round(sum(p.realized_pnl),2), count(*) FILTER (WHERE p.realized_pnl>0)
        FROM journal.positions p WHERE p.origin='scanner' AND p.status='CLOSED' AND p.qty_initial>0
          AND EXISTS (SELECT 1 FROM journal.scanner_counterfactuals c WHERE c.position_id=p.position_id) {where}""")
    n, actual, w = await cur.fetchone()
    print(f"Scanner counterfactuals, {n} closed trades{f' (last {days} days)' if days else ''}: actual {actual:+.2f} ({w} winners)")
    print(f"{'variant':12s} {'n':>4s} {'pnl':>10s} {'sumR':>8s} {'winners':>8s} {'vs base':>10s}")
    for v, nn, pnl, r, ww, vs in rows:
        print(f"{v:12s} {nn:4d} {float(pnl):+10.2f} {float(r):+8.2f} {ww:8d} {float(vs):+10.2f}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="print the variant totals after processing")
    ap.add_argument("--days", type=int, default=None, help="report window in days (default all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    from common.db import get_pool, close_pool
    from c1_ingestion.heartbeat import set_health
    pool = await get_pool()
    done, skipped = [], []
    async with pool.connection() as conn:
        ids = await pending_ids(conn)
        for pid in ids:
            if args.dry_run:
                print(f"would process position {pid}"); continue
            try:
                r = await process(conn, pid)
            except Exception as exc:                     # noqa: BLE001
                skipped.append((pid, str(exc))); continue
            (done if r else skipped).append(r or (pid, "no bars"))
        for r in done:
            print(f"position {r['pid']:4d} {r['ticker']:6s} actual {r['actual']:+8.2f}  " +
                  "  ".join(f"{k} {r[k]:+8.2f}" for k in VARIANTS))
        for pid, why in skipped:
            print(f"position {pid}: skipped ({why})")
        if args.report:
            await report(conn, args.days)
    try:
        await set_health("scanner_cf", "OK" if not skipped else "WARN",
                         f"{len(done)} journaled, {len(skipped)} skipped")
    except Exception:                                    # noqa: BLE001
        pass
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
