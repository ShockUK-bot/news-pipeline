"""A11 — nightly measurement layer (v0.17.0). Deterministic, no model calls.

Runs after force-flat (a11-metrics.timer, 15:20 CT weekdays) and:
  1. journal.trade_metrics      one row per CLOSED position, every lane
                                (holding time, MAE/MFE in R, realised R, exit
                                efficiency, predicted vs realised magnitude,
                                target reached)
  2. journal.scanner_counterfactuals  the v0.16.0 exit-ladder variants for
                                scanner trades (folded in from the standalone
                                scanner-counterfactual job)
  3. journal.counterfactuals    POST_EXIT price paths: what the position did
                                over the two sessions after its final exit
  4. journal.guard_ledger       outcome_class / outcome_pnl_r for A12 verdicts
                                whose outcome is now known (SAVE / SHAKEOUT /
                                NEUTRAL, see metrics.classify_guard)
  5. journal.metric_rollups     DAY and WEEK aggregates that A7 and (later)
                                A9 read
Read only against the broker; every step is idempotent and backfills on the
first run. Usage: python -m a11_metrics.service [--report] [--days N] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from common.db import close_pool, get_pool
from common.journal import register_config_version
from common.log import get_logger, kv

from .metrics import (classify_guard, downsample, excursions, exit_efficiency,
                      post_exit_outcome, rate, realized_r, side_sign, target_price)

log = get_logger("a11.metrics")
COMPONENT = "metrics"
CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
MINUTE_BAR_MAX_HOURS = 80          # longer holds use daily bars
FORCED_LAYERS = ("STOP", "CATASTROPHE", "INVALIDATION", "REVIEW", "GUARD", "BREAKER")


def _md():
    from common.marketdata import AlpacaData
    return AlpacaData()


async def bars_between(md, ticker: str, start: datetime, end: datetime) -> list[dict]:
    """Minute bars for short spans, daily bars (one per session) otherwise."""
    if (end - start) <= timedelta(hours=MINUTE_BAR_MAX_HOURS):
        return await md.minute_bars(ticker, start, end)
    data = await md._get(f"/v2/stocks/{ticker}/bars",
                         {"timeframe": "1Day", "start": start.isoformat(),
                          "end": end.isoformat(), "limit": 400, "adjustment": "split"})
    return [md._bar(b) for b in (data.get("bars") or [])]


# ---------------------------------------------------------------- 1. trade_metrics
async def trade_metrics_pass(conn, md, dry: bool) -> list[dict]:
    cur = await conn.execute(
        """SELECT p.position_id, p.ticker, p.side, p.origin, p.opened_ts, p.closed_ts,
                  p.avg_entry, p.qty_initial, p.r_unit, p.realized_pnl, p.exit_policy,
                  EXISTS (SELECT 1 FROM journal.exits e WHERE e.position_id=p.position_id
                          AND e.exit_layer='TARGET') AS target_exit
           FROM journal.positions p
           WHERE p.status='CLOSED' AND p.qty_initial > 0 AND p.closed_ts IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM journal.trade_metrics t WHERE t.position_id=p.position_id)
           ORDER BY p.position_id""")
    rows = await cur.fetchall()
    out = []
    for (pid, ticker, side, origin, opened, closed, entry, qty, r_unit, pnl, policy, target_exit) in rows:
        policy = policy if isinstance(policy, dict) else json.loads(policy or "{}")
        r_unit = float(r_unit or 0)
        if r_unit <= 0:
            continue
        try:
            bars = await bars_between(md, ticker, opened, closed + timedelta(minutes=1))
        except Exception as exc:                                  # noqa: BLE001
            log.warning("bars failed", extra=kv(position_id=pid, error=repr(exc)[:120]))
            continue
        ex = excursions(bars, side, float(entry), r_unit)
        rr = realized_r(float(pnl), int(qty), r_unit)
        mag = policy.get("magnitude_est")
        tf = float((policy.get("realization") or {}).get("target_fraction") or 0.6)
        hit = bool(target_exit)
        if mag and ex["bars"]:
            tp = target_price(float(entry), side, tf, float(mag))
            hit = hit or (ex["fav_px"] >= tp if side_sign(side) > 0 else ex["fav_px"] <= tp)
        row = {"position_id": pid, "ticker": ticker, "origin": origin,
               "holding_seconds": int((closed - opened).total_seconds()),
               "mae_r": ex["mae_r"], "mfe_r": ex["mfe_r"], "realized_r": rr,
               "exit_efficiency": exit_efficiency(rr, ex["mfe_r"]),
               "magnitude_predicted": round(float(mag), 4) if mag else None,
               "magnitude_realized": round(abs(ex["fav_px"] / float(entry) - 1), 4) if ex["bars"] else None,
               "window_hit": hit if ex["bars"] else None}
        if not dry:
            await conn.execute(
                """INSERT INTO journal.trade_metrics
                   (position_id, holding_seconds, mae_r, mfe_r, realized_r, exit_efficiency,
                    magnitude_predicted, magnitude_realized, window_hit)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (position_id) DO NOTHING""",
                (pid, row["holding_seconds"], row["mae_r"], row["mfe_r"], row["realized_r"],
                 row["exit_efficiency"], row["magnitude_predicted"], row["magnitude_realized"],
                 row["window_hit"]))
        out.append(row)
    return out


# ---------------------------------------------------------------- 2. scanner variants
async def scanner_cf_pass(conn, dry: bool) -> list[dict]:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for sub in ("ops", os.path.join("ops", "tools")):
        p = os.path.join(root, sub)
        if p not in sys.path:
            sys.path.insert(0, p)
    from scanner_counterfactuals import pending_ids, process      # noqa: E402
    out = []
    for pid in await pending_ids(conn):
        if dry:
            out.append({"pid": pid}); continue
        try:
            r = await process(conn, pid)
        except Exception as exc:                                  # noqa: BLE001
            log.warning("scanner cf failed", extra=kv(position_id=pid, error=repr(exc)[:120]))
            continue
        if r:
            out.append(r)
    return out


# ---------------------------------------------------------------- 3. post-exit paths
async def post_exit_pass(conn, md, dry: bool, today: date) -> list[dict]:
    cur = await conn.execute(
        """SELECT e.exit_id, e.position_id, p.ticker, p.side, e.ts, e.price, p.r_unit
           FROM journal.exits e JOIN journal.positions p USING (position_id)
           WHERE NOT e.is_partial AND p.status='CLOSED' AND p.r_unit > 0
             AND e.ts::date <= %s
             AND NOT EXISTS (SELECT 1 FROM journal.counterfactuals c
                             WHERE c.kind='POST_EXIT' AND c.exit_id=e.exit_id)
           ORDER BY e.ts""", (today - timedelta(days=2),))
    rows = await cur.fetchall()
    out = []
    for exit_id, pid, ticker, side, ts, px, r_unit in rows:
        try:
            mins = await md.minute_bars(ticker, ts, ts.replace(hour=20, minute=0, second=0, microsecond=0))
            dailies = await bars_between(md, ticker, ts + timedelta(days=1) - timedelta(hours=ts.hour),
                                         ts + timedelta(days=6))
        except Exception as exc:                                  # noqa: BLE001
            log.warning("post-exit bars failed", extra=kv(exit_id=exit_id, error=repr(exc)[:120]))
            continue
        after = [b for b in dailies if b["ts"].date() > ts.date()][:2]
        if len(after) < 2:
            continue                       # second session not printed yet; next run
        pts = [(b["ts"], b["close"]) for b in mins] + [(b["ts"], b["close"]) for b in after]
        later = after[-1]["close"] if after else pts[-1][1]
        outcome = post_exit_outcome(side, float(px), float(later), float(r_unit))
        row = {"exit_id": exit_id, "position_id": pid, "ticker": ticker,
               "horizon": "2_sessions",
               "outcome_r": outcome}
        if not dry:
            await conn.execute(
                """INSERT INTO journal.counterfactuals
                   (kind, exit_id, ticker, anchor_ts, anchor_price, horizon_desc, path, outcome_r)
                   VALUES ('POST_EXIT', %s, %s, %s, %s, %s, %s::jsonb, %s)""",
                (exit_id, ticker, ts, px, row["horizon"], json.dumps(downsample(pts)), outcome))
        out.append(row)
    return out


# ---------------------------------------------------------------- 4. guard outcomes
async def guard_pass(conn, md, dry: bool, now: datetime, reclassify: bool = False) -> list[dict]:
    cur = await conn.execute(
        """SELECT g.guard_id, g.position_id, p.ticker, p.side, g.ts, g.recommended_action,
                  p.status, p.r_unit, p.closed_ts,
                  (SELECT sum(e.price*e.qty)/NULLIF(sum(e.qty),0) FROM journal.exits e
                    WHERE e.position_id=p.position_id AND e.ts >= g.ts) AS exit_px_after,
                  (d.payload->'context'->'price_action'->>'unrealized_r')::numeric AS ur
           FROM journal.guard_ledger g JOIN journal.positions p USING (position_id)
           LEFT JOIN journal.decisions d ON d.decision_id = g.decision_id
           WHERE (g.outcome_class IS NULL OR %s) AND p.r_unit > 0
             AND (p.status='CLOSED' OR g.ts < %s - interval '7 days')
           ORDER BY g.ts""", (reclassify, now))
    rows = await cur.fetchall()
    out = []
    for gid, pid, ticker, side, ts, action, status, r_unit, closed_ts, exit_px_after, ur in rows:
        try:
            near = await md.minute_bars(ticker, ts - timedelta(minutes=30), ts + timedelta(hours=18))
        except Exception as exc:                                  # noqa: BLE001
            log.warning("guard bars failed", extra=kv(guard_id=gid, error=repr(exc)[:120]))
            continue
        before = [b for b in near if b["ts"] <= ts]
        after = [b for b in near if b["ts"] > ts]
        at_verdict = before[-1]["close"] if before else (after[0]["open"] if after else None)
        if at_verdict is None:
            continue
        if status == "CLOSED" and exit_px_after is not None:
            final = float(exit_px_after)
        elif status == "CLOSED":
            cls, pnl = "NEUTRAL", 0.0          # verdict landed after the last exit
            final = None
        else:
            try:
                later = await bars_between(md, ticker, ts + timedelta(days=1), ts + timedelta(days=9))
            except Exception:                                     # noqa: BLE001
                continue
            if not later:
                continue
            final = later[min(4, len(later) - 1)]["close"]
        if final is not None:
            delta = side_sign(side) * (final - float(at_verdict)) / float(r_unit)
            cls, pnl = classify_guard(action, delta,
                                      unrealized_r=(float(ur) if ur is not None else None))
        row = {"guard_id": gid, "position_id": pid, "ticker": ticker, "action": action,
               "outcome_class": cls, "outcome_pnl_r": pnl}
        if not dry:
            await conn.execute(
                """UPDATE journal.guard_ledger SET outcome_class=%s, outcome_pnl_r=%s, classified_ts=now()
                   WHERE guard_id=%s""", (cls, pnl, gid))
        out.append(row)
    return out


# ---------------------------------------------------------------- 5. rollups
async def _scalar(conn, sql: str, params=()) -> Optional[float]:
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return None if row is None or row[0] is None else float(row[0])


async def rollups_for(conn, start: date, end: date, gran: str, config_version: str, dry: bool) -> dict:
    """Aggregates for [start, end] inclusive. Upserts one row per metric."""
    P = (start, end)
    m: dict[str, tuple[Optional[float], Optional[dict]]] = {}
    cur = await conn.execute(
        """SELECT p.origin, count(*), count(*) FILTER (WHERE p.realized_pnl > 0),
                  sum(t.realized_r), avg(t.realized_r), avg(t.exit_efficiency), avg(t.mfe_r), avg(t.mae_r),
                  sum(p.realized_pnl)
           FROM journal.positions p JOIN journal.trade_metrics t USING (position_id)
           WHERE p.status='CLOSED' AND p.closed_ts::date BETWEEN %s AND %s
           GROUP BY 1""", P)
    by_origin = {r[0]: {"trades": r[1], "winners": r[2], "sum_r": float(r[3] or 0),
                        "avg_r": float(r[4] or 0), "avg_exit_eff": (float(r[5]) if r[5] is not None else None),
                        "avg_mfe_r": float(r[6] or 0), "avg_mae_r": float(r[7] or 0), "pnl": float(r[8] or 0)}
                 for r in await cur.fetchall()}
    tot = sum(v["trades"] for v in by_origin.values())
    win = sum(v["winners"] for v in by_origin.values())
    m["trades_closed"] = (tot, by_origin)
    m["win_rate"] = (rate(win, tot), {k: rate(v["winners"], v["trades"]) for k, v in by_origin.items()})
    m["sum_r"] = (sum(v["sum_r"] for v in by_origin.values()) if tot else None,
                  {k: v["sum_r"] for k, v in by_origin.items()})
    m["realized_pnl"] = (sum(v["pnl"] for v in by_origin.values()) if tot else None,
                         {k: v["pnl"] for k, v in by_origin.items()})
    effs = [v["avg_exit_eff"] for v in by_origin.values() if v["avg_exit_eff"] is not None]
    m["exit_efficiency"] = (await _scalar(conn, """SELECT avg(t.exit_efficiency) FROM journal.trade_metrics t
                                                    JOIN journal.positions p USING (position_id)
                                                    WHERE p.closed_ts::date BETWEEN %s AND %s""", P),
                            {k: v["avg_exit_eff"] for k, v in by_origin.items()})
    cur = await conn.execute(
        """SELECT e.exit_layer, avg(t.exit_efficiency), count(*)
           FROM journal.exits e JOIN journal.positions p USING (position_id)
           JOIN journal.trade_metrics t USING (position_id)
           WHERE NOT e.is_partial AND p.closed_ts::date BETWEEN %s AND %s
           GROUP BY 1""", P)
    for layer, eff, n in await cur.fetchall():
        m[f"exit_efficiency:{layer}"] = ((float(eff) if eff is not None else None), {"n": n})
    cur = await conn.execute(
        """SELECT action, count(*) FROM journal.decisions
           WHERE stage='TRIAGE' AND ts::date BETWEEN %s AND %s GROUP BY 1""", P)
    tri = {a: n for a, n in await cur.fetchall()}
    m["triage_escalation_rate"] = (rate(tri.get("ESCALATE", 0), sum(tri.values())), tri)
    cur = await conn.execute(
        """SELECT action, count(*) FROM journal.decisions
           WHERE stage='GATE' AND action IN ('PASS','VETO') AND ts::date BETWEEN %s AND %s GROUP BY 1""", P)
    gate = {a: n for a, n in await cur.fetchall()}
    m["gate_pass_rate"] = (rate(gate.get("PASS", 0), sum(gate.values())), gate)
    cur = await conn.execute(
        """SELECT recommended_action, outcome_class, count(*), avg(outcome_pnl_r)
           FROM journal.guard_ledger WHERE outcome_class IS NOT NULL AND ts::date BETWEEN %s AND %s
           GROUP BY 1,2""", P)
    g = {}
    for act, cls, n, avg in await cur.fetchall():
        g.setdefault(act, {})[cls] = {"n": n, "avg_pnl_r": float(avg or 0)}
    classified = sum(x["n"] for a in g.values() for x in a.values())
    saves = sum(a.get("SAVE", {}).get("n", 0) for a in g.values())
    m["guard_save_rate"] = (rate(saves, classified), g)
    m["veto_counterfactual_avg_eod_pct"] = (await _scalar(conn, """
        SELECT avg(CASE WHEN direction='down' THEN (price_at_veto-price_eod) ELSE (price_eod-price_at_veto) END
                   / NULLIF(price_at_veto,0)) * 100
        FROM journal.gate_counterfactuals
        WHERE complete AND rule NOT IN ('eh_shadow','fade') AND veto_ts::date BETWEEN %s AND %s""", P), None)
    m["post_exit_avg_r"] = (await _scalar(conn, """
        SELECT avg(outcome_r) FROM journal.counterfactuals
        WHERE kind='POST_EXIT' AND anchor_ts::date BETWEEN %s AND %s""", P), None)
    cur = await conn.execute(
        """SELECT c.variant, sum(c.pnl) FROM journal.scanner_counterfactuals c
           JOIN journal.positions p USING (position_id)
           WHERE p.closed_ts::date BETWEEN %s AND %s GROUP BY 1""", P)
    var = {v: float(s) for v, s in await cur.fetchall()}
    if var:
        m["scanner_cf_noscale_vs_base"] = (var.get("noscale", 0) - var.get("base", 0), var)
    written = 0
    for metric, (value, breakdown) in m.items():
        if value is None and not breakdown:
            continue
        if not dry:
            await conn.execute(
                """INSERT INTO journal.metric_rollups (period_start, granularity, metric, value, breakdown, config_version)
                   VALUES (%s,%s,%s,%s,%s::jsonb,%s)
                   ON CONFLICT (period_start, granularity, metric)
                   DO UPDATE SET value=EXCLUDED.value, breakdown=EXCLUDED.breakdown,
                                 config_version=EXCLUDED.config_version""",
                (start, gran, metric, value, json.dumps(breakdown) if breakdown is not None else None,
                 config_version))
        written += 1
    return {"period_start": start.isoformat(), "granularity": gran, "metrics": written,
            "trades_closed": tot, "win_rate": m["win_rate"][0], "sum_r": m["sum_r"][0],
            "gate_pass_rate": m["gate_pass_rate"][0], "guard_save_rate": m["guard_save_rate"][0]}


async def rollups_pass(conn, today: date, days: int, config_version: str, dry: bool) -> list[dict]:
    out = []
    for i in range(days):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        out.append(await rollups_for(conn, d, d, "DAY", config_version, dry))
    week_start = today - timedelta(days=today.weekday())
    out.append(await rollups_for(conn, week_start, week_start + timedelta(days=6), "WEEK", config_version, dry))
    return out


# ---------------------------------------------------------------- main
async def run(args) -> dict:
    cfgv = await register_config_version("a11 metrics run")
    pool = await get_pool()
    md = _md()
    now = datetime.now(timezone.utc)
    today = now.astimezone(CT).date()
    summary: dict = {}
    async with pool.connection() as conn:
        tm = await trade_metrics_pass(conn, md, args.dry_run)
        summary["trade_metrics"] = len(tm)
        sc = await scanner_cf_pass(conn, args.dry_run)
        summary["scanner_cf"] = len(sc)
        pe = await post_exit_pass(conn, md, args.dry_run, today)
        summary["post_exit"] = len(pe)
        gd = await guard_pass(conn, md, args.dry_run, now, reclassify=bool(args.reclassify_guard))
        summary["guard_classified"] = len(gd)
        ro = await rollups_pass(conn, today, int(args.days), cfgv, args.dry_run)
        summary["rollups"] = len(ro)
        if args.report:
            print(f"A11 metrics {today} (dry run)" if args.dry_run else f"A11 metrics {today}")
            print(f"  trade_metrics +{len(tm)}  scanner_cf +{len(sc)}  post_exit +{len(pe)}  guard classified +{len(gd)}")
            for r in gd:
                print(f"    guard {r['guard_id']:4d} {r['ticker']:6s} {r['action']:12s} -> {r['outcome_class']:8s} {r['outcome_pnl_r']:+.2f}R")
            for r in pe:
                print(f"    post-exit {r['ticker']:6s} pos {r['position_id']:4d} {r['horizon']:10s} {r['outcome_r']:+.2f}R")
            for r in ro:
                print(f"    rollup {r['granularity']:4s} {r['period_start']} trades {r['trades_closed']} "
                      f"win {r['win_rate']} sumR {r['sum_r']} gate_pass {r['gate_pass_rate']} guard_save {r['guard_save_rate']}")
    try:
        from c1_ingestion.heartbeat import set_health
        await set_health(COMPONENT, "OK", ", ".join(f"{k} {v}" for k, v in summary.items()))
    except Exception as exc:                                      # noqa: BLE001
        log.warning("heartbeat failed", extra=kv(error=repr(exc)[:120]))
    await close_pool()
    log.info("a11 done", extra=kv(**summary))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--days", type=int, default=3, help="DAY rollups to (re)compute, default 3")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reclassify-guard", action="store_true",
                    help="re-run the guard classification on every row (after a rule change)")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
