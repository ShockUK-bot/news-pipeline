"""A9 — weekend review (v0.18.0). Saturday 09:00 CT (a9-review.timer).

1. Evaluate: every APPROVED proposal older than its window gets a verdict
   against its success metric from A11's rollups (status EVALUATED).
2. Evidence pack from journal.metric_rollups, trade_metrics, guard_ledger,
   gate_counterfactuals, scanner_counterfactuals, burst_events (last 4 weeks).
3. Candidates: deterministic rules (candidates.py); at most 3 proposals, the
   rest WATCH items with their sample counts.
4. Narrative: the heavy slot explains each proposal (optional; ships without).
5. Journal: journal.proposals (PROPOSED), a SYSTEM/A9 decision, one email via
   journal.outbox (kind WEEKEND_REVIEW). Heartbeat `review`.

Operator loop: reply "approve proposal N" (or reject). Claude Code applies
the change, then runs `python -m a9_review.service --approve N --config-version <hash>`
(or --reject N); the following Saturday A9 evaluates it. Nothing is ever
applied automatically.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from common.config import config_path, load_yaml
from common.db import get_pool, jb, close_pool
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv

from .candidates import evaluate, generate
from .narrative import NarrativeError, build_messages, schema, validate

log = get_logger("a9.review")
COMPONENT = "review"
KIND = "WEEKEND_REVIEW"
CT = ZoneInfo("America/Chicago")


# ---------------------------------------------------------------- evidence
async def _rows(conn, sql, params=()):
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


async def build_evidence(conn, weeks: int = 4) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).date()
    ev: dict = {"window_weeks": weeks, "since": since.isoformat()}
    lanes = {}
    for origin, n, w, sum_r, pnl, eff in await _rows(conn, """
        SELECT p.origin, count(*), count(*) FILTER (WHERE p.realized_pnl > 0),
               sum(t.realized_r), sum(p.realized_pnl), avg(t.exit_efficiency)
        FROM journal.positions p JOIN journal.trade_metrics t USING (position_id)
        WHERE p.status='CLOSED' AND p.closed_ts::date >= %s GROUP BY 1""", (since,)):
        lanes[origin] = {"trades": n, "winners": w, "sum_r": round(float(sum_r or 0), 2),
                         "pnl": round(float(pnl or 0), 2), "exit_eff": (round(float(eff), 3) if eff is not None else None)}
    ev["lanes"] = lanes
    layers = {}
    for layer, eff, n in await _rows(conn, """
        SELECT e.exit_layer, avg(t.exit_efficiency), count(*)
        FROM journal.exits e JOIN journal.positions p USING (position_id)
        JOIN journal.trade_metrics t USING (position_id)
        WHERE NOT e.is_partial AND p.closed_ts::date >= %s GROUP BY 1""", (since,)):
        layers[layer] = {"eff": (round(float(eff), 3) if eff is not None else None), "n": n}
    ev["exit_layers"] = layers
    ev["exit_eff_trail"] = (layers.get("TRAIL") or {}).get("eff")
    g = await _rows(conn, """
        SELECT count(DISTINCT position_id) FILTER (WHERE recommended_action='HOLD' AND outcome_class='SAVE'),
               count(DISTINCT position_id) FILTER (WHERE recommended_action='HOLD' AND outcome_class='SHAKEOUT'),
               count(*) FILTER (WHERE outcome_class='SAVE'), count(*) FILTER (WHERE outcome_class IS NOT NULL),
               count(*) FILTER (WHERE recommended_action='EXIT' AND outcome_class='SAVE'),
               count(*) FILTER (WHERE recommended_action='EXIT' AND outcome_class='SHAKEOUT')
        FROM journal.guard_ledger WHERE ts::date >= %s""", (since,))
    hs, hsh, saves, classified, es, esh = g[0]
    ev["guard"] = {"hold_save_positions": hs, "hold_shakeout_positions": hsh,
                   "save_rate": (round(saves / classified, 3) if classified else None),
                   "classified": classified, "exit_saves": es, "exit_shakeouts": esh}
    ev["veto_cf"] = [{"veto_reason": r, "measured": n, "avg_best_pct": float(b or 0), "avg_eod_pct": float(e or 0)}
                     for r, n, b, e in await _rows(conn, """
        SELECT veto_reason, count(*),
               avg(CASE WHEN direction='down' THEN max_down_pct ELSE max_up_pct END) * 100,
               avg(CASE WHEN direction='down' THEN (price_at_veto-price_eod) ELSE (price_eod-price_at_veto) END
                   / NULLIF(price_at_veto,0)) * 100
        FROM journal.gate_counterfactuals
        WHERE complete AND rule NOT IN ('eh_shadow','fade') AND veto_ts::date >= %s
        GROUP BY 1 HAVING count(*) >= 5 ORDER BY 2 DESC""", (since,))]
    cf = {}
    for variant, n, pnl, winners in await _rows(conn, """
        SELECT variant, count(*), sum(pnl), count(*) FILTER (WHERE pnl > 0)
        FROM journal.scanner_counterfactuals GROUP BY 1"""):
        cf[variant] = {"n": n, "pnl": round(float(pnl), 2), "winners": winners}
    if cf:
        ev["scanner_cf"] = {"n": cf["base"]["n"], "base": cf["base"]["pnl"], "noscale": cf.get("noscale", {}).get("pnl"),
                            "noscale_vs_base": (round(cf["noscale"]["pnl"] - cf["base"]["pnl"], 2) if "noscale" in cf else None),
                            "stop3_vs_base": (round(cf["stop3.0"]["pnl"] - cf["base"]["pnl"], 2) if "stop3.0" in cf else None),
                            "base_winners": cf["base"]["winners"], "noscale_winners": cf.get("noscale", {}).get("winners")}
    b = await _rows(conn, """
        SELECT count(*), avg((detail->'realistic'->>'ret_30m')::numeric) * 100
        FROM journal.burst_events
        WHERE complete AND rule='fade' AND direction='down' AND detail ? 'realistic'
          AND NOT coalesce((detail->>'repeat')::boolean, false)
          AND NOT coalesce((detail->>'fade_oversize')::boolean, false) AND ts::date >= %s""", (since,))
    n, avg = b[0]
    ev["burst"] = {"real_n": n, "real_avg_30m_pct": (round(float(avg), 3) if avg is not None else None),
                   "real_after_cost": (round(float(avg) - 0.10, 3) if avg is not None else None)}
    # v0.23.0: scanner funnel counterfactuals (A11 funnel pass)
    try:
        from a11_metrics.funnel import summary as funnel_summary
        ev["funnel"] = await funnel_summary(conn, weeks * 7)
    except Exception as exc:                                      # noqa: BLE001
        log.warning("funnel evidence unavailable", extra=kv(error=repr(exc)[:120]))
        ev["funnel"] = {}
    # v0.24.0: shadow lanes (fade, drift_short) from the gate counterfactuals
    ev["shadow_lanes"] = {}
    for rule in ("fade", "drift_short"):
        rows = await _rows(conn, """
            SELECT count(*), avg(x.r) * 100, percentile_cont(0.5) WITHIN GROUP (ORDER BY x.r) * 100,
                   100.0 * count(*) FILTER (WHERE x.r > 0) / NULLIF(count(*), 0),
                   count(DISTINCT x.d), count(DISTINCT x.d) FILTER (WHERE x.r < 0)
            FROM (SELECT veto_ts::date AS d,
                         (price_at_veto - price_eod) / NULLIF(price_at_veto, 0) AS r
                  FROM journal.gate_counterfactuals
                  WHERE rule=%s AND veto_reason='WOULD_TRADE' AND complete
                    AND veto_ts::date >= %s) x""", (rule, since))
        n, avg, med, win, sess, neg = rows[0]
        # sessions negative counts sessions whose MEAN is negative
        neg_rows = await _rows(conn, """
            SELECT count(*) FROM (SELECT veto_ts::date, avg((price_at_veto - price_eod) / NULLIF(price_at_veto, 0)) AS m
                                  FROM journal.gate_counterfactuals
                                  WHERE rule=%s AND veto_reason='WOULD_TRADE' AND complete AND veto_ts::date >= %s
                                  GROUP BY 1) s WHERE m < 0""", (rule, since))
        ev["shadow_lanes"][rule] = {"n": n, "avg_eod_pct": (round(float(avg), 3) if avg is not None else None),
                                    "median_eod_pct": (round(float(med), 3) if med is not None else None),
                                    "win_pct": (round(float(win), 1) if win is not None else None),
                                    "sessions": sess, "neg_sessions": int(neg_rows[0][0])}
    ev["week_rollups"] = [{"period_start": ps.isoformat(), "metric": m, "value": (float(v) if v is not None else None)}
                          for ps, m, v in await _rows(conn, """
        SELECT period_start, metric, value FROM journal.metric_rollups
        WHERE granularity='WEEK' AND period_start >= %s AND metric IN
              ('trades_closed','win_rate','sum_r','realized_pnl','exit_efficiency','gate_pass_rate',
               'triage_escalation_rate','guard_save_rate','veto_counterfactual_avg_eod_pct','post_exit_avg_r')
        ORDER BY 1, 2""", (since,))]
    return ev


async def latest_rollup(conn, metric: str, granularity: str, after: datetime) -> Optional[float]:
    rows = await _rows(conn, """
        SELECT value FROM journal.metric_rollups
        WHERE metric=%s AND granularity=%s AND period_start >= %s::date
        ORDER BY period_start DESC LIMIT 1""", (metric, granularity, after))
    return float(rows[0][0]) if rows and rows[0][0] is not None else None


# ---------------------------------------------------------------- evaluation
async def evaluate_pass(conn, dry: bool) -> list[dict]:
    out = []
    for pid, title, metric, reviewed in await _rows(conn, """
        SELECT proposal_id, title, success_metric, reviewed_ts FROM journal.proposals
        WHERE status='APPROVED' AND reviewed_ts < now() - interval '6 days' ORDER BY 1"""):
        try:
            m = json.loads(metric)
        except (TypeError, ValueError):
            m = {}
        observed = await latest_rollup(conn, m.get("metric", ""), m.get("granularity", "WEEK"), reviewed)
        verdict = evaluate(metric, observed)
        if verdict["verdict"] == "NO_DATA":
            continue
        if not dry:
            await conn.execute("""UPDATE journal.proposals SET status='EVALUATED', evaluation=%s
                                  WHERE proposal_id=%s""", (jb(verdict), pid))
        out.append({"proposal_id": pid, "title": title, **verdict})
    return out


# ---------------------------------------------------------------- narrative
async def narrate(backend, proposal: dict, retries: int):
    if backend is None:
        return None, None, 0
    err, total = None, 0
    for _ in range(1 + retries):
        try:
            reply = await backend.complete(build_messages(proposal, err.detail if err else None), schema())
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            log.warning("narrative call failed", extra=kv(error=repr(e)[:200]))
            return None, None, total
        total += reply.latency_ms
        try:
            return validate(reply.text), reply.model_id, total
        except NarrativeError as e:
            err = e
    return None, backend.model_id, total


# ---------------------------------------------------------------- render
def render_html(week: str, proposals: list[dict], watch: list[str], evaluated: list[dict], ev: dict) -> str:
    from common import mailkit as mk
    lanes = ev.get("lanes") or {}
    headline = (f"{len(proposals)} proposal{'s' if len(proposals) != 1 else ''} this week"
                + (f", {len(evaluated)} evaluated" if evaluated else "")
                + (". Reply 'approve proposal N' to act." if proposals else ". Nothing crossed its evidence bar."))
    tiles = mk.tiles([(f"{k}", f'<span style="color:{mk.colour(v.get("pnl"))}">{mk.money(v.get("pnl"))}</span>'
                       f'<div style="font-size:11px;color:{mk.MUTED};font-weight:400">{v.get("trades")} trades, {v.get("winners")} winners, {v.get("sum_r"):+.1f}R</div>', None)
                      for k, v in lanes.items()]) if lanes else ""
    blocks = [tiles]
    if evaluated:
        blocks.append(mk.section("Evaluations of approved proposals",
                                 mk.table(["#", "Title", "Verdict", "Observed", "Target"],
                                          [[str(e["proposal_id"]), mk.esc(e["title"]), mk.esc(e["verdict"]), mk.esc(e.get("observed")),
                                            mk.esc(f"{e.get('op', '')} {e.get('target', '')}")] for e in evaluated])))
    for p in proposals:
        ev_rows = [[mk.esc(k), mk.esc(v)] for k, v in (p.get("evidence") or {}).items() if k != "source"]
        inner = (f"<div style='font-size:13px;margin:4px 0'><b>Current:</b> {mk.esc(p['current_state'])}</div>"
                 f"<div style='font-size:13px;margin:4px 0'><b>Change:</b> {mk.esc(p['proposed_diff'])}</div>"
                 f"<div style='font-size:13px;margin:4px 0'><b>Expected:</b> {mk.esc(p['expected_effect'])}</div>"
                 + mk.table(["Evidence", "Value"], ev_rows) +
                 f"<div style='font-size:12px;color:{mk.MUTED};margin-top:6px'>Success test: {mk.esc(p['success_metric'])}</div>")
        if p.get("narrative"):
            inner += (f"<div style='font-size:13px;margin-top:8px'><b>Why:</b> {mk.esc(p['narrative']['rationale'])}</div>"
                      f"<div style='font-size:13px;margin-top:4px'><b>Risk:</b> {mk.esc(p['narrative']['risk'])}</div>")
        inner += (f"<div style='margin-top:10px;font-size:13px'>To act: reply <code>approve proposal {p['proposal_id']}</code> "
                  f"or <code>reject proposal {p['proposal_id']}</code>.</div>")
        blocks.append(mk.section(f"#{p['proposal_id']}  {p['title']}", inner))
    if watch:
        blocks.append(mk.section("Watch list", mk.bullets(watch), note="below the evidence bar; the counts show how far each sample is"))
    wr = [[mk.esc(r["period_start"]), mk.esc(r["metric"]), mk.esc(round(r["value"], 3) if r["value"] is not None else "—")]
          for r in (ev.get("week_rollups") or [])[-24:]]
    if wr:
        blocks.append(mk.section("Weekly rollups", mk.table(["Week", "Metric", "Value"], wr)))
    return mk.page("Weekend review", headline, blocks, "news-pipeline A9 weekend review (evidence rules; nothing is applied automatically)", week)


def render(week: str, proposals: list[dict], watch: list[str], evaluated: list[dict], ev: dict) -> str:
    L = [f"A9 weekend review, week of {week}", ""]
    L.append(f"Evidence window: last {ev['window_weeks']} weeks (since {ev['since']}).")
    lanes = ev.get("lanes") or {}
    if lanes:
        L.append("Lanes: " + "; ".join(f"{k} {v['trades']} trades, {v['winners']} winners, {v['sum_r']:+.1f}R, {v['pnl']:+.0f} USD"
                                       for k, v in lanes.items()))
    L.append("")
    if evaluated:
        L.append("Evaluations of approved proposals:")
        for e in evaluated:
            L.append(f"  #{e['proposal_id']} {e['title']}: {e['verdict']} ({e.get('metric')} observed {e.get('observed')} vs target {e.get('op')} {e.get('target')})")
        L.append("")
    if proposals:
        L.append(f"Proposals ({len(proposals)}). To act on one, tell Claude Code: approve proposal N (or reject).")
        for p in proposals:
            L.append(f"\n#{p['proposal_id']}  {p['title']}")
            L.append(f"  Current:  {p['current_state']}")
            L.append(f"  Change:   {p['proposed_diff']}")
            L.append(f"  Evidence: {json.dumps(p['evidence'], default=str)}")
            L.append(f"  Expected: {p['expected_effect']}")
            L.append(f"  Success:  {p['success_metric']}")
            if p.get("narrative"):
                L.append(f"  Why:      {p['narrative']['rationale']}")
                L.append(f"  Risk:     {p['narrative']['risk']}")
    else:
        L.append("No proposal this week: nothing crossed its evidence bar.")
    if watch:
        L.append("\nWatch list (below the evidence bar):")
        for w in watch:
            L.append(f"  - {w}")
    L.append("\nNothing is applied automatically. A9 evaluates approved proposals the following Saturday.")
    return "\n".join(L)


# ---------------------------------------------------------------- run
async def run_review(cfg: dict, dry: bool = False, backend_override=None) -> Optional[int]:
    await register_config_version("a9 weekend review")
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    week = (now.astimezone(CT).date() - timedelta(days=now.astimezone(CT).weekday())).isoformat()
    rcfg = cfg.get("review") or {}
    async with pool.connection() as conn:
        evaluated = await evaluate_pass(conn, dry)
        ev = await build_evidence(conn, int(rcfg.get("evidence_weeks", 4)))
        proposals, watch = generate(ev, int(rcfg.get("max_proposals", 3)))
        # skip proposals already open for the same rule
        open_rules = {r[0] for r in await _rows(conn, """
            SELECT evidence->>'rule' FROM journal.proposals WHERE status IN ('PROPOSED','APPROVED')""")}
        fresh = []
        for p in proposals:
            if p["rule"] in open_rules:
                watch.append(f"(already open) {p['title']}")
            else:
                fresh.append(p)
        proposals = fresh
    backend, slot_name, slots = backend_override, "stub", None
    if backend_override is None and proposals and (rcfg.get("narrative", True)) and not dry:
        from a7_report.service import SlotManager
        slots = SlotManager(cfg)
        backend, slot_name = await slots.acquire()
    try:
        for p in proposals:
            n, model_id, latency = await narrate(backend, p, int((cfg.get("narrative") or {}).get("retries_on_invalid", 1)))
            p["narrative"] = n.model_dump() if n else None
            p["model_id"], p["latency_ms"] = model_id, latency
    finally:
        if slots is not None:
            await slots.release()
    if dry:
        for p in proposals:
            p["proposal_id"] = 0
        print(render(week, proposals, watch, evaluated, ev))
        await close_pool()
        return None
    async with pool.connection() as conn:
        async with conn.transaction():
            for p in proposals:
                evidence = dict(p["evidence"], rule=p["rule"])
                cur = await conn.execute(
                    """INSERT INTO journal.proposals
                       (title, current_state, proposed_diff, evidence, expected_effect, success_metric)
                       VALUES (%s,%s,%s,%s,%s,%s) RETURNING proposal_id""",
                    (p["title"], p["current_state"], p["proposed_diff"], jb(evidence),
                     p["expected_effect"], p["success_metric"]))
                p["proposal_id"] = (await cur.fetchone())[0]
            body = render(week, proposals, watch, evaluated, ev)
            subject = f"A9 weekend review {week}: {len(proposals)} proposal(s), {len(evaluated)} evaluated"
            decision_id = await write_decision(
                signal_id=f"a9-{week}", stage="SYSTEM", agent="A9", action="REVIEW",
                payload={"week": week, "proposals": [{k: p[k] for k in ("proposal_id", "title", "rule", "success_metric")} for p in proposals],
                         "watch": watch, "evaluated": evaluated, "slot": slot_name,
                         "lanes": ev.get("lanes"), "guard": ev.get("guard")},
                reason=subject, conn=conn)
            cur = await conn.execute(
                """INSERT INTO journal.outbox (kind, subject, body, html, fact_sheet, decision_id)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING message_id""",
                (KIND, subject, body, render_html(week, proposals, watch, evaluated, ev),
                 jb({"proposals": [p["proposal_id"] for p in proposals], "watch": watch}), decision_id))
            outbox_id = (await cur.fetchone())[0]
    try:
        from c1_ingestion.heartbeat import set_health
        await set_health(COMPONENT, "OK", f"{len(proposals)} proposals, {len(watch)} watch, {len(evaluated)} evaluated")
    except Exception as exc:                                      # noqa: BLE001
        log.warning("heartbeat failed", extra=kv(error=repr(exc)[:120]))
    log.info("review queued", extra=kv(outbox_id=outbox_id, proposals=len(proposals), watch=len(watch),
                                       evaluated=len(evaluated), slot=slot_name))
    await close_pool()
    return outbox_id


async def set_status(proposal_id: int, status: str, config_version: Optional[str]) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        if config_version:
            await conn.execute(
                """INSERT INTO journal.config_versions (config_version, summary, proposal_id)
                   VALUES (%s, %s, %s) ON CONFLICT (config_version) DO UPDATE SET proposal_id=EXCLUDED.proposal_id""",
                (config_version, f"proposal {proposal_id} applied", proposal_id))
        await conn.execute(
            """UPDATE journal.proposals SET status=%s, reviewed_ts=now(), config_version_result=%s
               WHERE proposal_id=%s""", (status, config_version, proposal_id))
        await write_decision(signal_id=f"a9-proposal-{proposal_id}", stage="SYSTEM", agent="A9",
                             action=f"PROPOSAL_{status}",
                             payload={"proposal_id": proposal_id, "config_version": config_version},
                             reason=f"operator {status.lower()} proposal {proposal_id}", conn=conn)
    await close_pool()
    print(f"proposal {proposal_id}: {status}" + (f" at {config_version}" if config_version else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the review, journal nothing, no model")
    ap.add_argument("--approve", type=int, metavar="N")
    ap.add_argument("--reject", type=int, metavar="N")
    ap.add_argument("--config-version", help="commit hash the approved proposal was applied in")
    args = ap.parse_args()
    if args.approve:
        asyncio.run(set_status(args.approve, "APPROVED", args.config_version)); return
    if args.reject:
        asyncio.run(set_status(args.reject, "REJECTED", None)); return
    cfg = load_yaml(config_path("a9.yaml"))
    asyncio.run(run_review(cfg, dry=args.dry_run))


if __name__ == "__main__":
    main()
