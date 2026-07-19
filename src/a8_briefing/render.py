"""Deterministic plain-text rendering of the consolidated morning
briefing. Pure function — no I/O, no model. Every section renders from
whatever facts exist; missing sections say so instead of vanishing, so the
operator can tell "quiet" from "broken"."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
RULE = "=" * 62
rule = "-" * 62


def _t(iso: str | None) -> str:
    if not iso:
        return "?"
    return datetime.fromisoformat(iso).astimezone(CT).strftime("%H:%M CT")


def subject_line(facts: dict) -> str:
    d = facts["session_date"]
    a4 = facts.get("a4") or {}
    n_cand = a4.get("open_candidates", 0) or 0
    recos = ((facts.get("a6") or {}).get("review") or {}).get(
        "recommendations", 0) or 0
    parts = [f"{n_cand} candidate{'s' if n_cand != 1 else ''}"]
    if recos:
        parts.append(f"{recos} position reco{'s' if recos != 1 else ''}")
    black = [p for p in facts.get("positions") or [] if p.get("blackout_soon")]
    if black:
        parts.append(f"{len(black)} earnings-window position"
                     f"{'s' if len(black) != 1 else ''}")
    return f"Morning briefing {d} — " + ", ".join(parts)


def render(facts: dict, narrative) -> str:
    L: list[str] = []
    L.append(f"MORNING BRIEFING — {facts['session_date']}")
    L.append(RULE)

    if narrative is not None:
        L.append(narrative.summary)
        for w in narrative.watch_items:
            L.append(f"  * {w[:150]}")
    else:
        L.append("(narrative unavailable — facts below are unaffected)")
    L.append(RULE)

    # --- A4 sheet ----------------------------------------------------------
    a4 = facts.get("a4")
    if a4 is None:
        L.append("PRE-MARKET SHEET: not available yet this morning — check "
                 "a4-premarket logs if this persists past 07:20 ET.")
    else:
        L.append(f"PRE-MARKET SHEET ({a4.get('open_candidates', 0)} open "
                 f"candidates, analyst evaluates at {_t(a4.get('entry_ts'))})")
        if a4.get("summary"):
            L.append(f"  {a4['summary']}")
        for c in a4.get("open_forwarded") or []:
            L.append(f"  #{c.get('rank')} {','.join(c.get('tickers') or ['?'])}"
                     f" — {(c.get('headline') or c.get('item_id') or '?')[:76]}")
        L.append(f"  Overnight: {a4.get('fresh', 0)} fresh / "
                 f"{a4.get('guard_routed', 0)} to guard / "
                 f"{a4.get('thesis_routed', 0)} to thesis lane / "
                 f"{a4.get('ignored', 0)} ignored.")
    L.append(rule)

    # --- positions + A6 ----------------------------------------------------
    positions = facts.get("positions") or []
    a6r = (facts.get("a6") or {}).get("review") or {}
    recos = {r.get("position_id"): r for r in (a6r.get("recos") or [])}
    L.append(f"OPEN POSITIONS ({len(positions)})")
    if not positions:
        L.append("  None.")
    for p in positions:
        rp = p.get("r_progress")
        line = (f"  {p['ticker']:<6} [{p['horizon']}] "
                f"{p['qty_open']} sh @ {p['avg_entry']}"
                f" | R {rp if rp is not None else '?'}"
                f" | stop {p.get('current_stop')}")
        if p.get("blackout_soon"):
            line += (f" | EARNINGS in {p['earnings_next_sessions']} "
                     f"session{'s' if p['earnings_next_sessions'] != 1 else ''}")
        L.append(line)
        reco = recos.get(p["position_id"])
        if reco:
            L.append(f"      A6 recommends {reco.get('action')}: "
                     f"{(reco.get('rationale') or '')[:110]}")
    if a6r:
        L.append(f"  Last A6 review ({a6r.get('run_date', '?')}): "
                 f"{a6r.get('reviewed', 0)} reviewed, "
                 f"{a6r.get('recommendations', 0)} recommendations, "
                 f"{a6r.get('stale_flagged', 0)} stale-flagged.")
    else:
        L.append("  No A6 review on record yet.")
    L.append(rule)

    # --- theses ------------------------------------------------------------
    th = facts.get("thesis") or {}
    active = th.get("active") or []
    L.append(f"STANDING THESES ({len(active)} active)")
    for t in active[:6]:
        tickers = ",".join(b.get("ticker", "?")
                           for b in (t.get("beneficiaries") or [])[:4])
        L.append(f"  {t['thesis_id']} [{t.get('direction')}, "
                 f"conf {t.get('confidence'):.2f}] {t.get('title', '')[:52]}"
                 f" ({tickers})")
    if len(active) > 6:
        L.append(f"  ... and {len(active) - 6} more")
    dg = th.get("digest")
    if dg:
        L.append(f"  Last A5 pass ({dg.get('run_date', '?')}): "
                 f"{dg.get('new_theses', 0)} new, "
                 f"{dg.get('evidence_attached', 0)} evidence, "
                 f"{dg.get('status_changes', 0)} status changes.")
    L.append(rule)

    # --- earnings ----------------------------------------------------------
    e = facts.get("earnings") or {}
    total = e.get("reporting_today")
    L.append("EARNINGS: "
             + (f"{total} US names report today."
                if total is not None else "calendar unavailable."))
    for h in e.get("held_reporting_soon") or []:
        L.append(f"  HELD name reporting soon: {h['ticker']} on "
                 f"{h['report_date']}")
    L.append(rule)

    # --- ops ---------------------------------------------------------------
    ops = facts.get("ops") or {}
    q = ops.get("queues") or {}
    L.append(f"SYSTEM: queues analyst={q.get('signal.analyst', 0)} "
             f"guard={q.get('signal.guard', 0)} "
             f"overnight={q.get('signal.overnight', 0)} "
             f"thesis={q.get('signal.thesis', 0)}; newest item "
             f"{ops.get('newest_item_age_hours', '?')}h old.")
    bad = ops.get("health_not_ok") or []
    if bad:
        for b in bad:
            L.append(f"  HEALTH {b['status']}: {b['component']} — "
                     f"{b['detail']}")
    else:
        L.append("  All health components OK.")
    L.append(RULE)
    L.append("Generated by A8. All numbers by code from the journal; "
             "narrative by model. Gates, sizing, and exits unchanged.")
    return "\n".join(L)
