"""Evening digest (v0.22.0), 21:20 CT weekdays. ONE email that replaces the
four evening ones (A7 EOD report 15:37, A6 review 19:01, A5 thesis digest
20:35, C11 entry plan 21:15). Nothing here calls a model: it composes what
those agents already journaled today, adds the day's numbers and a "needs
you" box, and renders HTML with a plain-text fallback. If A7 did not run
(holiday) the digest is skipped and journaled SKIPPED_NO_SESSION.

Run: python -m evening_digest.service [--dry-run] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from common.db import close_pool, get_pool, jb
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from common import mailkit as mk

log = get_logger("evening.digest")
KIND = "EVENING_DIGEST"
CT = ZoneInfo("America/Chicago")


async def _one(conn, sql, params=()):
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


async def _all(conn, sql, params=()):
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


def _j(x):
    return x if isinstance(x, (dict, list)) or x is None else json.loads(x)


async def gather(conn, day: date) -> dict:
    d = {"date": day.isoformat()}
    row = await _one(conn, """SELECT payload FROM journal.decisions WHERE stage='SYSTEM' AND agent='A7'
                              AND action='REPORT' AND payload->>'session_date'=%s ORDER BY ts DESC LIMIT 1""", (day.isoformat(),))
    d["a7"] = _j(row[0]) if row else None
    row = await _one(conn, """SELECT payload FROM journal.decisions WHERE agent='A6' AND action='REVIEW'
                              AND payload->>'run_date'=%s ORDER BY ts DESC LIMIT 1""", (day.isoformat(),))
    d["a6"] = _j(row[0]) if row else None
    row = await _one(conn, """SELECT payload FROM journal.decisions WHERE agent='A5' AND action='DIGEST'
                              AND payload->>'run_date'=%s ORDER BY ts DESC LIMIT 1""", (day.isoformat(),))
    d["a5"] = _j(row[0]) if row else None
    row = await _one(conn, """SELECT payload FROM journal.decisions WHERE agent='C11' AND action='THESIS_PLAN'
                              AND payload->>'run_date'=%s ORDER BY ts DESC LIMIT 1""", (day.isoformat(),))
    d["c11"] = _j(row[0]) if row else None
    d["skips"] = [(r[0], r[1]) for r in await _all(conn, """
        SELECT ticker, payload->>'skip' FROM journal.decisions WHERE agent='C11' AND action='THESIS_SKIP'
        AND payload->>'run_date'=%s ORDER BY ts""", (day.isoformat(),))]
    d["positions"] = [dict(zip(("ticker", "side", "origin", "sector", "qty", "entry", "last", "stop", "r_unit", "opened"), r))
                      for r in await _all(conn, """
        SELECT p.ticker, p.side, p.origin, s.sector, p.qty_open, p.avg_entry, p.last_price,
               COALESCE((p.exit_policy->>'current_stop')::numeric,
                        (p.exit_policy->'initial_stop'->>'price')::numeric, p.initial_stop), p.r_unit, p.opened_ts
        FROM journal.positions p LEFT JOIN journal.sectors s USING (ticker)
        WHERE p.status='OPEN' ORDER BY p.opened_ts""")]
    d["week_realized"] = float((await _one(conn, """
        SELECT COALESCE(sum(realized_pnl),0) FROM journal.exits
        WHERE ts >= date_trunc('week', %s::date) AND ts::date <= %s""", (day, day)))[0])
    d["month_realized"] = float((await _one(conn, """
        SELECT COALESCE(sum(realized_pnl),0) FROM journal.exits
        WHERE ts >= date_trunc('month', %s::date) AND ts::date <= %s""", (day, day)))[0])
    d["guard_auto"] = [dict(zip(("ticker", "action", "ts", "taken"), r)) for r in await _all(conn, """
        SELECT p.ticker, g.recommended_action, g.ts, g.action_taken FROM journal.guard_ledger g
        JOIN journal.positions p USING (position_id)
        WHERE g.ts::date=%s AND g.auto_executed ORDER BY g.ts""", (day,))]
    d["lane_day"] = [dict(zip(("origin", "n", "pnl"), r)) for r in await _all(conn, """
        SELECT p.origin, count(*), sum(e.realized_pnl) FROM journal.exits e JOIN journal.positions p USING (position_id)
        WHERE e.ts::date=%s GROUP BY 1 ORDER BY 3 DESC""", (day,))]
    d["equity"] = (await _one(conn, "SELECT value FROM journal.control WHERE key='broker_equity'") or [None])[0]
    return d


def compose(d: dict) -> tuple[str, str, str, list[str]]:
    """(subject, text, html, needs_you) from the gathered dict. Pure."""
    a7 = d.get("a7") or {}
    facts = a7.get("facts") or {}
    trades = facts.get("trades") or {}
    exits = trades.get("exits") or []
    opened = trades.get("opened") or []
    realized = float(trades.get("realized_pnl_today") or 0)
    positions = d.get("positions") or []
    unreal = 0.0
    for p in positions:
        if p.get("last") and p.get("entry"):
            m = -1 if str(p["side"]).upper() == "SHORT" else 1
            unreal += m * (float(p["last"]) - float(p["entry"])) * int(p["qty"])
    n_full = sum(1 for e in exits if not e.get("is_partial"))
    needs: list[str] = []
    c11 = d.get("c11") or {}
    for x in c11.get("dead_armed") or []:
        needs.append(f"{x.get('ticker')}: {str(x.get('status','')).replace('_',' ').lower()} armed, C4 sells at 09:35 ET tomorrow (backup stop {x.get('stop')}).")
    for x in c11.get("trim_recos") or []:
        needs.append(f"{x.get('ticker')}: trim recommended before earnings.")
    a6 = d.get("a6") or {}
    for r in a6.get("recos") or []:
        if r.get("action") in ("EXIT_RECO", "TRIM_RECO"):
            needs.append(f"{r.get('ticker')}: A6 {str(r.get('action')).replace('_RECO','').lower()} recommendation ({str(r.get('rationale',''))[:110]}).")
    for g in d.get("guard_auto") or []:
        needs.append(f"{g['ticker']}: guard auto-exit armed at {g['ts'].astimezone(CT).strftime('%H:%M')} CT ({g['taken']}).")
    for h in facts.get("health_not_ok") or []:
        needs.append(f"Health: {h.get('component')} is {h.get('status')} ({str(h.get('detail',''))[:80]}).")
    for p in positions:
        if p.get("stop") is None:
            needs.append(f"{p['ticker']}: no current stop recorded on the open position.")
    tone = "Up" if realized > 0 else "Down" if realized < 0 else "Flat"
    headline = (f"{tone} {mk.money(realized, signed=False)} today on {n_full} closed trade{'s' if n_full != 1 else ''}"
                f"{', ' + str(len(opened)) + ' opened' if opened else ''}. "
                + (f"{len(needs)} item{'s' if len(needs) != 1 else ''} need you." if needs else "Nothing needs you."))
    subject = f"Evening {d['date']}: {tone.lower()} {mk.money(realized, signed=False)}, {n_full} trades" + (f", {len(needs)} to decide" if needs else "")
    t = [("Realized today", mk.money(realized), realized),
         ("Week to date", mk.money(d.get("week_realized")), d.get("week_realized")),
         ("Month to date", mk.money(d.get("month_realized")), d.get("month_realized")),
         ("Open positions", str(len(positions)), None),
         ("Unrealized", mk.money(unreal), unreal),
         ("Equity", mk.money(d.get("equity"), signed=False) if d.get("equity") else "—", None)]
    blocks = [mk.tiles(t), mk.needs_you(needs)]
    rows = [[mk.esc(e.get("ticker")), mk.esc(e.get("layer")) + (" (half)" if e.get("is_partial") else ""),
             str(e.get("qty")), mk.num(e.get("price")), mk.num(e.get("r_multiple"), 2) + "R",
             f'<span style="color:{mk.colour(e.get("realized_pnl"))}">{mk.money(e.get("realized_pnl"))}</span>',
             mk.esc(str(e.get("ts", ""))[11:16])] for e in exits]
    lane = ", ".join(f"{l['origin']} {mk.money(l['pnl'])} ({l['n']} exits)" for l in d.get("lane_day") or [])
    blocks.append(mk.section("Trades today", mk.table(["Ticker", "Exit", "Qty", "Price", "R", "P&L", "Time"], rows,
                                                     "no exits today", {2, 3, 4, 5}), note=lane or None))
    if opened:
        orow = [[mk.esc(o.get("ticker")), mk.esc(o.get("horizon") or o.get("origin") or ""), str(o.get("qty_initial") or o.get("qty") or ""),
                 mk.esc(str(o.get("headline") or "")[:70])] for o in opened]
        blocks.append(mk.section("Opened today", mk.table(["Ticker", "Lane", "Qty", "Why"], orow)))
    prow = []
    for p in positions:
        m = -1 if str(p["side"]).upper() == "SHORT" else 1
        u = m * (float(p["last"] or p["entry"]) - float(p["entry"])) * int(p["qty"])
        rp = (m * (float(p["last"] or p["entry"]) - float(p["entry"])) / float(p["r_unit"])) if p.get("r_unit") else None
        prow.append([mk.esc(p["ticker"]), mk.side_chip(p["side"]), mk.esc(p.get("origin")), mk.esc(p.get("sector") or "—"),
                     str(p["qty"]), mk.num(p["entry"]), mk.num(p["last"]), mk.num(p["stop"]) if p.get("stop") is not None else "—",
                     f'<span style="color:{mk.colour(u)}">{mk.money(u)}</span>', (mk.num(rp) + "R") if rp is not None else "—"])
    blocks.append(mk.section("Open positions", mk.table(["Ticker", "Side", "Lane", "Sector", "Qty", "Entry", "Last", "Stop", "Unrl", "R"],
                                                        prow, "flat", {4, 5, 6, 7, 8, 9})))
    plan_rows = [[mk.esc(x.get("ticker")), mk.esc(x.get("thesis_id")), str(x.get("qty")), mk.num(x.get("limit_price"))]
                 for x in c11.get("planned_detail") or []]
    skips = d.get("skips") or []
    plan_note = (f"{len(plan_rows)} entries planned for tomorrow; {len(skips)} skipped"
                 + (": " + ", ".join(f"{t} ({s.lower()})" for t, s in skips[:8]) if skips else "")) if c11 else "C11 did not run"
    blocks.append(mk.section("Thesis plan for tomorrow", mk.table(["Ticker", "Thesis", "Qty", "Limit"], plan_rows, "no new entries"), note=plan_note))
    a5 = d.get("a5") or {}
    if a5:
        a5_note = (f"{a5.get('new_theses', 0)} new, {a5.get('status_changes', 0)} status changes, "
                   f"{a5.get('evidence_attached', 0)} evidence attached, {a5.get('active_after', '?')} active")
        blocks.append(mk.section("Thesis store", mk.bullets([str(a5.get("summary", ""))[:400]]), note=a5_note))
    verdicts = (facts.get("guard") or {}).get("verdicts") or []
    if verdicts or a6:
        grow = [[mk.esc(v.get("ticker")), mk.esc(v.get("recommended_action")), mk.esc(v.get("urgency")),
                 "yes" if v.get("thesis_intact") else "no", mk.esc(str(v.get("ts", ""))[11:16])] for v in verdicts]
        a6_note = (f"A6 nightly: {a6.get('reviewed', 0)} reviewed, {a6.get('recommendations', 0)} recommendations, "
                   f"{a6.get('stale_flagged', 0)} stale") if a6 else None
        blocks.append(mk.section("Guard and review", mk.table(["Ticker", "Verdict", "Urgency", "Thesis intact", "Time"], grow, "no guard verdicts today"), note=a6_note))
    act = facts.get("activity") or {}
    vet = act.get("vetoes") or []
    vrows = []
    for v in (vet if isinstance(vet, list) else []):
        if isinstance(v, dict):
            vrows.append([mk.esc(v.get("stage")), mk.esc(v.get("veto_reason") or v.get("reason")), str(v.get("count") or v.get("n") or "")])
        elif isinstance(v, (list, tuple)) and len(v) >= 3:
            vrows.append([mk.esc(v[0]), mk.esc(v[1]), str(v[2])])
    gaps = facts.get("ingestion_gaps") or []
    ops_note = f"{act.get('items_ingested', '?')} items ingested; {len(gaps)} ingestion gap{'s' if len(gaps) != 1 else ''}"
    blocks.append(mk.section("Pipeline", mk.table(["Stage", "Veto reason", "Count"], vrows[:8], "no vetoes"), note=ops_note))
    narr = a7.get("narrative") or {}
    if narr:
        blocks.append(mk.section("Operator's log", "<div style='font-size:13px;line-height:1.5'>" + mk.esc(narr.get("summary", "")) + "</div>" + mk.bullets(narr.get("notables") or [])))
    html = mk.page("Evening digest", headline, blocks, "news-pipeline evening digest (A7, A6, A5, C11 journaled today; no model call)",
                   d["date"])
    text = [headline, ""]
    text += [f"Realized today {mk.money(realized)} | week {mk.money(d.get('week_realized'))} | month {mk.money(d.get('month_realized'))} | open {len(positions)} | unrealized {mk.money(unreal)}", ""]
    if needs:
        text += ["NEEDS YOU:"] + [f"  - {n}" for n in needs] + [""]
    text += ["TRADES TODAY:"] + ([f"  {e.get('ticker')} {e.get('layer')} {e.get('qty')} @ {e.get('price')} {mk.money(e.get('realized_pnl'))}" for e in exits] or ["  none"]) + [""]
    text += ["OPEN POSITIONS:"] + ([f"  {p['ticker']} {p['side']} {p['qty']} @ {p['entry']} last {p['last']} stop {p['stop']} sector {p.get('sector') or '-'}" for p in positions] or ["  flat"]) + [""]
    text += [f"THESIS PLAN: {plan_note}", ""]
    if narr:
        text += ["LOG: " + str(narr.get("summary", ""))] + [f"  - {n}" for n in narr.get("notables") or []]
    return subject, "\n".join(text) + "\n", html, needs


async def run(day: date | None = None, dry: bool = False) -> int | None:
    await register_config_version("evening digest")
    pool = await get_pool()
    day = day or datetime.now(CT).date()
    async with pool.connection() as conn:
        d = await gather(conn, day)
        if not d.get("a7"):
            if not dry:
                await write_decision(signal_id=f"evening-{day.isoformat()}", stage="SYSTEM", agent="DIGEST",
                                     action="SKIPPED_NO_SESSION", payload={"date": day.isoformat()},
                                     reason="no A7 report today", conn=conn)
            log.info("no A7 report today; digest skipped", extra=kv(date=day.isoformat()))
            await close_pool()
            return None
        subject, text, html, needs = compose(d)
        if dry:
            print(subject); print(text); print(f"[html {len(html)} bytes]")
            await close_pool()
            return None
        async with conn.transaction():
            decision_id = await write_decision(
                signal_id=f"evening-{day.isoformat()}", stage="SYSTEM", agent="DIGEST", action="DIGEST",
                payload={"date": day.isoformat(), "needs_you": needs, "subject": subject,
                         "sources": {k: bool(d.get(k)) for k in ("a7", "a6", "a5", "c11")}},
                reason=subject, conn=conn)
            cur = await conn.execute(
                """INSERT INTO journal.outbox (kind, subject, body, html, fact_sheet, decision_id)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING message_id""",
                (KIND, subject, text, html, jb({"needs_you": needs}), decision_id))
            outbox_id = (await cur.fetchone())[0]
    try:
        from c1_ingestion.heartbeat import set_health
        await set_health("digest", "OK", f"queued outbox {outbox_id}, {len(needs)} needs-you")
    except Exception as e:                                        # noqa: BLE001
        log.warning("heartbeat failed", extra=kv(error=repr(e)[:120]))
    log.info("evening digest queued", extra=kv(outbox_id=outbox_id, needs=len(needs)))
    await close_pool()
    return outbox_id


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date")
    a = ap.parse_args()
    asyncio.run(run(date.fromisoformat(a.date) if a.date else None, dry=a.dry_run))


if __name__ == "__main__":
    main()
