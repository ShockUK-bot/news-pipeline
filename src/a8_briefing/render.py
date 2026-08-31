"""Deterministic plain-text rendering of the consolidated morning
briefing. Pure function — no I/O, no model. Every section renders from
whatever facts exist; missing sections say so instead of vanishing, so the
operator can tell "quiet" from "broken".

v0.14.4 applies that same principle to the pipeline itself. An outage banner
now leads the email and the subject line, because on 2026-08-27 a1-triage
stopped and the only trace was one yellow line in the SYSTEM block at the
very bottom, below candidates, positions and earnings. It was mailed four
mornings running and read as background. A dead pipeline is not a footnote."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
RULE = "=" * 62
rule = "-" * 62


def _span(minutes: float) -> str:
    """Human units. Nobody reacts to '5102.3min'; everybody reacts to
    '3.5 days'."""
    if minutes < 180:
        return f"{minutes:.0f} min"
    if minutes < 2880:
        return f"{minutes / 60:.1f} hours"
    return f"{minutes / 1440:.1f} days"


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
    base = f"Morning briefing {d} — " + ", ".join(parts)
    out = (facts.get("ops") or {}).get("outages") or []
    if out:
        w = out[0]
        lead = f"[OUTAGE] {w['component']} stale {_span(w['age_min'])}"
        if len(out) > 1:
            lead += f" +{len(out) - 1} more"
        return f"{lead} — {base}"
    return base


def render(facts: dict, narrative) -> str:
    L: list[str] = []
    L.append(f"MORNING BRIEFING — {facts['session_date']}")
    L.append(RULE)

    # v0.14.4: outages lead. Above the narrative, above everything.
    outages = (facts.get("ops") or {}).get("outages") or []
    if outages:
        L.append("*** PIPELINE OUTAGE — READ THIS FIRST ***")
        for o in outages:
            line = (f"  {o['component']}: no heartbeat for "
                    f"{_span(o['age_min'])} "
                    f"(limit {o['max_age_min']:.0f} min)")
            if o.get("desc"):
                line += f" — {o['desc']}"
            L.append(line)
            if o.get("unit"):
                L.append(f"      fix: sudo systemctl restart {o['unit']}")
        L.append("  A component that stops writing its heartbeat is not "
                 "quiet. It is not running.")
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
        line = (f"  {p['ticker']:<6} "
                f"[{p.get('side', 'LONG')} {p['horizon']}] "
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
