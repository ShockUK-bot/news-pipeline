"""A9 candidate rules. Pure functions over an evidence dict (built by the
service from A11's tables). Every rule states its minimum sample; below it
the finding is a WATCH item, not a proposal. Each proposal names ONE change,
the current state, the evidence and a success metric A9 evaluates the
following weekend (baseline §8: one parameter, one week, one verdict)."""
from __future__ import annotations

import json
from typing import Optional


def metric_json(metric: str, op: str, target: float, baseline: Optional[float],
                granularity: str = "WEEK", weeks: int = 2) -> str:
    return json.dumps({"metric": metric, "granularity": granularity, "op": op,
                       "target": target, "baseline": baseline, "weeks": weeks})


def _p(title, current, diff, evidence, effect, metric):
    return {"title": title, "current_state": current, "proposed_diff": diff,
            "evidence": evidence, "expected_effect": effect, "success_metric": metric}


def rule_scanner_no_scale_out(ev: dict) -> tuple[Optional[dict], Optional[str]]:
    s = ev.get("scanner_cf") or {}
    n = int(s.get("n") or 0)
    delta = s.get("noscale_vs_base")
    if n < 30:
        return None, (f"scanner no-scale-out: {n} of 30 trades journaled"
                      + (f", noscale vs base {delta:+.0f}" if delta is not None else ""))
    if delta is None or delta <= 0 or int(s.get("noscale_winners") or 0) < int(s.get("base_winners") or 0) - 2:
        return None, f"scanner no-scale-out: n={n}, noscale vs base {delta:+.0f}, not better"
    return _p("Scanner scalp: drop the 50% scale-out, let the full size ride the 1.5 ATR trail",
              f"config/exit_profiles.yaml scalp_v1.realization: {{target_fraction: 0.6, action: scale_out_50}}",
              "scalp_v1.realization.action: scale_out_50 -> none (target still journaled)",
              {"n_instances": n, "noscale_vs_base_usd": delta, "base_pnl": s.get("base"),
               "noscale_pnl": s.get("noscale"), "source": "journal.scanner_counterfactuals"},
              f"Over the last {n} scanner trades the full-size trail would have made {delta:+.0f} more than the current ladder.",
              metric_json("exit_efficiency:TRAIL", ">=", 0.55, ev.get("exit_eff_trail"))), None


def rule_guard_hold_bias(ev: dict) -> tuple[Optional[dict], Optional[str]]:
    g = ev.get("guard") or {}
    sp, hp = int(g.get("hold_save_positions") or 0), int(g.get("hold_shakeout_positions") or 0)
    tot = sp + hp
    if tot < 12:
        return None, f"guard HOLD bias: {tot} of 12 positions classified"
    share = hp / tot
    if share < 0.65:
        return None, f"guard HOLD bias: shakeout share {share:.0%} across {tot} positions, under the 65% bar"
    return _p("Guard: lean tighten_stop on HOLD verdicts for under-water positions",
              "src/a12_guard/prompt.py: hold is 'most common'; no rule for a position already below entry",
              "Prompt rule: when price_action.unrealized_r <= -0.5 and the item is negative for the side, prefer tighten_stop over hold",
              {"n_instances": tot, "hold_shakeout_positions": hp, "hold_save_positions": sp,
               "share": round(share, 3), "source": "journal.guard_ledger (A11 classified)"},
              "Fewer positions ride from a small loss to a full stop while each item is judged noise.",
              metric_json("guard_save_rate", ">=", 0.5, g.get("save_rate"))), None


def rule_gate_money_left(ev: dict) -> tuple[Optional[dict], Optional[str]]:
    best = None
    for r in ev.get("veto_cf") or []:
        if int(r.get("measured") or 0) >= 20 and (r.get("avg_best_pct") or 0) >= 1.5 and (r.get("avg_eod_pct") or 0) >= 0.5:
            if best is None or r["avg_eod_pct"] > best["avg_eod_pct"]:
                best = r
    if best is None:
        return None, "gate money-left: no veto reason with 20+ measured, best >= 1.5% and EOD >= 0.5% in its direction"
    return _p(f"Gate: review the {best['veto_reason']} veto, the vetoed trades are winning",
              f"config/gate.yaml rule behind {best['veto_reason']} (unchanged)",
              f"Shadow {best['veto_reason']} for one week (journal WOULD_TRADE instead of VETO) or loosen its threshold one notch",
              {"n_instances": best["measured"], "veto_reason": best["veto_reason"],
               "avg_best_pct": best["avg_best_pct"], "avg_eod_pct": best["avg_eod_pct"],
               "source": "journal.gate_counterfactuals (direction adjusted)"},
              f"Vetoed {best['veto_reason']} trades averaged {best['avg_eod_pct']:+.2f}% to the close in their direction.",
              metric_json("veto_counterfactual_avg_eod_pct", "<=", 0.5, best["avg_eod_pct"])), None


def rule_stop_layer_inefficiency(ev: dict) -> tuple[Optional[dict], Optional[str]]:
    e = ev.get("exit_layers") or {}
    st = e.get("STOP") or {}
    n = int(st.get("n") or 0)
    eff = st.get("eff")
    if n < 10 or eff is None:
        return None, f"stop exits: {n} of 10 measured"
    if eff > -0.5:
        return None, f"stop exits: efficiency {eff:+.2f} over {n}, above the -0.5 bar"
    cf = ev.get("scanner_cf") or {}
    s3 = cf.get("stop3_vs_base")
    return _p("Exits: stop-outs are giving back more than they ever had; review the 2.0 ATR initial stop",
              "config/exit_profiles.yaml scalp_v1.initial_stop.k: 2.0 (news/thesis: 2.0 / 3.0 daily ATR)",
              "Test 3.0 ATR(5m) on the scanner lane in shadow (journaled variant already exists) before changing k",
              {"n_instances": n, "exit_efficiency_stop": eff, "stop3_vs_base_usd": s3,
               "source": "journal.metric_rollups exit_efficiency:STOP, journal.scanner_counterfactuals"},
              "Fewer first-hour chop stop-outs; larger loss per stop-out. Net effect is what the variant journal shows.",
              metric_json("exit_efficiency:STOP", ">=", -0.3, eff)), None


def rule_burst_go_live(ev: dict) -> tuple[Optional[dict], Optional[str]]:
    b = ev.get("burst") or {}
    n = int(b.get("real_n") or 0)
    ev_cost = b.get("real_after_cost")
    if n < 200:
        return None, f"burst fade: {n} of 200 realistic rows" + (f", after cost {ev_cost:+.3f}%" if ev_cost is not None else "")
    if ev_cost is None or ev_cost < 0.15:
        return None, f"burst fade: n={n}, after cost {ev_cost:+.3f}%, under the +0.15% bar"
    return _p("Burst fade: build the lane (C12 to A3, 0.5/0.5 bracket, 10 minute time stop)",
              "config/burst.yaml: research only, no order path",
              "New lane per claude_burst-target-review-2026-09-16.md section 4",
              {"n_instances": n, "real_after_cost_pct": ev_cost, "source": "journal.burst_events detail.realistic"},
              f"{n} realistic rows at {ev_cost:+.3f}% after cost clear the go-live bar.",
              metric_json("realized_pnl", ">", 0, None)), None


def rule_lane_negative(ev: dict) -> tuple[Optional[dict], Optional[str]]:
    worst = None
    for lane, r in (ev.get("lanes") or {}).items():
        if int(r.get("trades") or 0) >= 10 and (r.get("sum_r") or 0) <= -3.0:
            if worst is None or r["sum_r"] < worst[1]["sum_r"]:
                worst = (lane, r)
    if worst is None:
        return None, "lane P&L: no lane with 10+ trades and -3R or worse in the window"
    lane, r = worst
    return _p(f"{lane} lane: halve its risk until it shows a positive four weeks",
              f"config/risk.yaml capital.risk_per_trade_pct 0.005 applies to the {lane} lane",
              f"Lane multiplier 0.5 for origin={lane} (A3 scanner_capital_cfg pattern), or pause the lane's entries",
              {"n_instances": r["trades"], "sum_r": r["sum_r"], "winners": r.get("winners"),
               "pnl": r.get("pnl"), "source": "journal.trade_metrics x journal.positions"},
              f"The {lane} lane lost {r['sum_r']:+.1f}R over {r['trades']} trades; halving risk halves the bleed while it is measured.",
              metric_json("sum_r", ">", 0, r["sum_r"])), None


def rule_scanner_concurrency(ev: dict) -> tuple[Optional[dict], Optional[str]]:
    """v0.23.0: the concurrency cap costs money if the with-move trades it
    blocked would have made more than +3R over 10 or more instances."""
    f = ((ev.get("funnel") or {}).get("by_outcome") or {}).get("CAPPED_CONCURRENT") or {}
    n = int(f.get("n") or 0)
    r = f.get("with_move_r")
    if n < 10:
        return None, f"scanner concurrency cap: {n} of 10 capped instances journaled" + (f", with-move {r:+.1f}R" if r is not None else "")
    if r is None or r <= 3.0:
        return None, f"scanner concurrency cap: n={n}, with-move {r:+.1f}R, the cap is not costing money"
    return _p("Scanner: raise the concurrency cap from 2 to 3",
              "config/risk.yaml scanner.max_concurrent_positions: 2",
              "scanner.max_concurrent_positions: 2 -> 3 (A3 veto SCANNER_CONCURRENT)",
              {"n_instances": n, "with_move_sum_r": r, "winners": f.get("with_move_winners"),
               "source": "journal.scanner_funnel_cf outcome=CAPPED_CONCURRENT"},
              f"The {n} entries blocked by the cap would have made {r:+.1f}R trading with the move.",
              metric_json("sum_r", ">", 0, None)), None


def rule_analyst_short_bias(ev: dict) -> tuple[Optional[dict], Optional[str]]:
    """v0.23.0: on scanner up-moves the analyst sometimes proposes a short
    (an exhaustion fade) which the gate then vetoes on structure. If the
    with-move long beats the proposed short by 2R or more over 8 or more
    cases, propose forcing the momentum direction on the scanner lane."""
    a = (ev.get("funnel") or {}).get("analyst_short_on_up_move") or {}
    n = int(a.get("n") or 0)
    if n < 8:
        return None, f"analyst shorts on scanner up-moves: {n} of 8 cases journaled"
    gap = float(a.get("long_sum_r") or 0) - float(a.get("short_sum_r") or 0)
    if gap < 2.0:
        return None, f"analyst shorts on scanner up-moves: n={n}, long beats short by {gap:+.1f}R, under the 2R bar"
    return _p("Scanner: analyst must trade WITH the detected move (no exhaustion shorts on up-moves)",
              "src/a2_analyst prompt: the analyst chooses direction on scanner items",
              "Scanner items: direction fixed to the scanner's move direction; the analyst only decides trade / no trade",
              {"n_instances": n, "long_sum_r": a.get("long_sum_r"), "short_sum_r": a.get("short_sum_r"),
               "long_better": a.get("long_better"), "source": "journal.scanner_funnel_cf (move up, analyst down)"},
              f"Over {n} cases the with-move long made {a.get('long_sum_r'):+.1f}R against {a.get('short_sum_r'):+.1f}R for the proposed shorts.",
              metric_json("sum_r", ">", 0, None)), None


RULES = [rule_lane_negative, rule_gate_money_left, rule_stop_layer_inefficiency,
         rule_guard_hold_bias, rule_scanner_no_scale_out, rule_scanner_concurrency,
         rule_analyst_short_bias, rule_burst_go_live]


def generate(ev: dict, max_proposals: int = 3) -> tuple[list[dict], list[str]]:
    proposals, watch = [], []
    for rule in RULES:
        p, w = rule(ev)
        if p and len(proposals) < max_proposals:
            p["rule"] = rule.__name__
            proposals.append(p)
        elif w:
            watch.append(w)
        elif p:
            watch.append(f"(deferred, cap reached) {p['title']}")
    return proposals, watch


def evaluate(success_metric: str, observed: Optional[float]) -> dict:
    """Verdict of a proposal's success metric against the observed rollup."""
    try:
        m = json.loads(success_metric)
    except (TypeError, ValueError):
        return {"verdict": "UNPARSEABLE", "observed": observed}
    if observed is None:
        return {"verdict": "NO_DATA", "metric": m.get("metric"), "observed": None, "target": m.get("target")}
    op, t = m.get("op", ">="), float(m.get("target", 0))
    ok = {">=": observed >= t, ">": observed > t, "<=": observed <= t, "<": observed < t}.get(op, False)
    return {"verdict": "MET" if ok else "NOT_MET", "metric": m.get("metric"),
            "observed": observed, "target": t, "op": op, "baseline": m.get("baseline")}
