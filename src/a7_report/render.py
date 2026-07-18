"""Deterministic plain-text rendering of the EOD report. Pure function of
(facts, narrative) — fully unit-testable, no I/O, no model. The email is
readable with the narrative section absent (model offline) or present."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

RULE = "-" * 62


def _money(x) -> str:
    if x is None:
        return "n/a"
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def _t(iso: str | None) -> str:
    if not iso:
        return "n/a"
    dt = datetime.fromisoformat(iso)
    return dt.astimezone(CT).strftime("%H:%M CT")


def subject_line(facts: dict) -> str:
    d = facts["session_date"]
    n_open = len(facts["trades"]["opened"])
    n_exit = len(facts["trades"]["exits"])
    pnl = facts["trades"]["realized_pnl_today"]
    if n_open == 0 and n_exit == 0:
        return f"EOD Report {d} — no trades"
    return (f"EOD Report {d} — {n_open} opened / {n_exit} exits, "
            f"realized {_money(pnl)}")


def render(facts: dict, narrative=None) -> str:
    L: list[str] = []
    a = facts["activity"]
    t = facts["trades"]

    L.append(f"END-OF-DAY REPORT — {facts['session_date']} (paper trading)")
    L.append(RULE)

    if narrative is not None:
        L.append(narrative.summary)
        if narrative.notables:
            L.append("")
            L.append("Notable:")
            for n in narrative.notables:
                L.append(f"  * {n}")
        if narrative.data_quality != "ok":
            L.append(f"  (narrative flags data quality: {narrative.data_quality})")
    else:
        L.append("(narrative unavailable — model offline; all numbers below "
                 "are code-computed and unaffected)")
    L.append(RULE)

    # --- trades -------------------------------------------------------------
    L.append("TRADES")
    if not t["opened"] and not t["exits"]:
        L.append("  No positions opened or exited today.")
    for p in t["opened"]:
        L.append(f"  OPENED {p['ticker']} {p['qty']} @ {_money(p['avg_entry'])} "
                 f"({p['horizon']}, stop {_money(p['initial_stop'])}, "
                 f"{_t(p['opened_ts'])})")
        if p.get("headline"):
            L.append(f"         trigger: {p['headline'][:90]}")
    for e in t["exits"]:
        part = " (partial)" if e["is_partial"] else ""
        L.append(f"  EXIT   {e['ticker']} {e['qty']} @ {_money(e['price'])} "
                 f"via {e['layer']}{part}: {_money(e['realized_pnl'])} "
                 f"({e['r_multiple']:+.2f}R, {_t(e['ts'])})")
    L.append(f"  Realized P&L today: {_money(t['realized_pnl_today'])}")
    if t["pnl_by_exit_layer"]:
        mix = ", ".join(f"{x['layer']}×{x['count']} {_money(x['realized_pnl'])}"
                        for x in t["pnl_by_exit_layer"])
        L.append(f"  By exit layer: {mix}")
    L.append(RULE)

    # --- open positions ------------------------------------------------------
    L.append("OPEN POSITIONS")
    if not facts["open_positions"]:
        L.append("  None (flat).")
    for p in facts["open_positions"]:
        ur = (f"{p['unrealized_r']:+.2f}R" if p["unrealized_r"] is not None
              else "n/a")
        L.append(f"  {p['ticker']} {p['qty_open']} @ {_money(p['avg_entry'])} "
                 f"last {_money(p['last_price'])} ({ur}, "
                 f"{_money(p['unrealized_pnl'])}) stop "
                 f"{_money(p['current_stop'])} [{p['stop_basis'] or 'initial'}] "
                 f"{p['horizon']}")
    L.append(RULE)

    # --- guard ---------------------------------------------------------------
    g = facts["guard"]
    L.append("POSITION GUARD (A12)")
    if not g["verdicts"] and g["alert_only_count"] == 0:
        L.append("  No position-touching news evaluated today.")
    for v in g["verdicts"]:
        intact = "thesis intact" if v["thesis_intact"] else "THESIS BROKEN"
        L.append(f"  {v['ticker']}: {intact} -> {v['recommended_action']} "
                 f"({v['urgency']}, {_t(v['ts'])})")
    if g["alert_only_count"]:
        L.append(f"  ALERT-ONLY (model was down): {g['alert_only_count']} — "
                 "check the dashboard tape")
    L.append(RULE)

    # --- pipeline & vetoes ---------------------------------------------------
    sc = a["stage_counts"]
    tri = sc.get("TRIAGE", {})
    L.append("PIPELINE")
    L.append(f"  Items ingested: {a['items_ingested']}   "
             f"triage: {tri.get('ESCALATE', 0)} escalated / "
             f"{tri.get('DISCARD', 0)} discarded / "
             f"{tri.get('SUPPRESS', 0)} suppressed")
    L.append(f"  Theses: {sc.get('ANALYST', {}).get('THESIS', 0)}   "
             f"gate passes: {sc.get('GATE', {}).get('PASS', 0)}   "
             f"orders: {sum(t['orders_by_role'].values())}")
    if a["vetoes"]:
        L.append("  Vetoes: " + ", ".join(
            f"{v['reason']}×{v['count']}" for v in a["vetoes"]))
    if a["quarantined_today"]:
        L.append(f"  Quarantined items today: {a['quarantined_today']}")
    L.append(RULE)

    # --- system --------------------------------------------------------------
    c = facts["controls"]
    L.append("SYSTEM")
    flags = []
    for key, label in (("kill_switch", "KILL SWITCH"),
                       ("drawdown_breaker", "BREAKER"),
                       ("block_entries", "ENTRIES BLOCKED")):
        if c.get(key) == "1":
            flags.append(label)
    L.append("  Controls: " + (" | ".join(flags) if flags else "all clear")
             + f"   capital ${float(c.get('trading_capital', 0)):,.0f}"
             + f"   max trades/day {c.get('max_trades_per_day', '?')}")
    if facts["health_not_ok"]:
        for h in facts["health_not_ok"]:
            L.append(f"  HEALTH {h['status']}: {h['component']} — "
                     f"{(h['detail'] or '')[:70]}")
    else:
        L.append("  Health: all components OK")
    for gp in facts["ingestion_gaps"]:
        end = _t(gp["end"]) if gp["end"] else "ONGOING"
        L.append(f"  Ingestion gap: {gp['source']} {_t(gp['start'])} -> {end}")
    L.append(RULE)
    L.append("Generated by A7. Numbers computed by code from the journal; "
             "narrative (if present) by the heavy model slot.")
    return "\n".join(L)
