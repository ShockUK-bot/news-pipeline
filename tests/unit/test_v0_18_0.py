"""v0.18.0: A9 candidate rules, success-metric evaluation, narrative schema, wiring."""
import json
from pathlib import Path

import yaml

from a9_review.candidates import RULES, evaluate, generate, metric_json
from a9_review.narrative import build_messages, validate

ROOT = Path(__file__).resolve().parents[2]


def ev(**over):
    base = {"window_weeks": 4, "since": "2026-08-20",
            "lanes": {"scanner": {"trades": 17, "winners": 11, "sum_r": 7.0, "pnl": 1378.0},
                      "news": {"trades": 12, "winners": 4, "sum_r": -3.5, "pnl": -900.0}},
            "exit_layers": {"STOP": {"eff": -0.56, "n": 12}, "TRAIL": {"eff": 0.57, "n": 9}},
            "exit_eff_trail": 0.57,
            "guard": {"hold_save_positions": 7, "hold_shakeout_positions": 11, "save_rate": 0.12, "classified": 157},
            "veto_cf": [{"veto_reason": "GATE_NO_CONFIRM", "measured": 40, "avg_best_pct": 2.1, "avg_eod_pct": 0.8}],
            "scanner_cf": {"n": 17, "base": 1646.0, "noscale": 2071.0, "noscale_vs_base": 425.0,
                           "stop3_vs_base": 14.0, "base_winners": 11, "noscale_winners": 10},
            "burst": {"real_n": 73, "real_avg_30m_pct": 0.06, "real_after_cost": -0.04}}
    base.update(over)
    return base


def test_generate_orders_rules_and_caps():
    props, watch = generate(ev(), max_proposals=3)
    rules = [p["rule"] for p in props]
    assert rules[:3] == ["rule_lane_negative", "rule_gate_money_left", "rule_stop_layer_inefficiency"]
    assert any("scanner no-scale-out: 17 of 30" in w for w in watch)
    assert any("burst fade: 73 of 200" in w for w in watch)
    assert any("guard HOLD bias" in w and "61%" in w for w in watch)
    for p in props:
        for k in ("title", "current_state", "proposed_diff", "evidence", "expected_effect", "success_metric"):
            assert p[k]
        json.loads(p["success_metric"])


def test_rules_stay_quiet_without_evidence():
    quiet = ev(lanes={"scanner": {"trades": 5, "winners": 4, "sum_r": 3.0, "pnl": 500.0}},
               exit_layers={"STOP": {"eff": -0.1, "n": 4}}, veto_cf=[], scanner_cf={"n": 3, "noscale_vs_base": 10.0},
               guard={"hold_save_positions": 2, "hold_shakeout_positions": 3, "save_rate": 0.3})
    props, watch = generate(quiet)
    assert props == [] and len(watch) == len(RULES)


def test_scanner_no_scale_out_fires_at_30():
    props, _ = generate(ev(scanner_cf={"n": 32, "base": 3000.0, "noscale": 3600.0, "noscale_vs_base": 600.0,
                                       "stop3_vs_base": 0.0, "base_winners": 20, "noscale_winners": 19},
                           lanes={}, veto_cf=[], exit_layers={}, guard={}), max_proposals=5)
    assert [p["rule"] for p in props] == ["rule_scanner_no_scale_out"]
    assert "scale_out_50 -> none" in props[0]["proposed_diff"]


def test_evaluate_metric():
    m = metric_json("exit_efficiency:TRAIL", ">=", 0.55, 0.5)
    assert evaluate(m, 0.6)["verdict"] == "MET"
    assert evaluate(m, 0.5)["verdict"] == "NOT_MET"
    assert evaluate(m, None)["verdict"] == "NO_DATA"
    assert evaluate("not json", 1.0)["verdict"] == "UNPARSEABLE"
    assert evaluate(metric_json("sum_r", ">", 0, -3.5), 0.1)["verdict"] == "MET"


def test_narrative_schema_and_messages():
    p = generate(ev())[0][0]
    msgs = build_messages(p)
    assert msgs[0]["role"] == "system" and p["title"] in msgs[1]["content"]
    n = validate(json.dumps({"rationale": "The news lane lost -3.5R over 12 trades.", "risk": "Halving risk halves the upside too."}))
    assert n.rationale.startswith("The news lane")
    try:
        validate(json.dumps({"rationale": "x", "risk": "y", "extra": 1}))
        assert False, "extra keys must be rejected"
    except Exception:
        pass


def test_wiring():
    cfg = yaml.safe_load((ROOT / "config" / "a9.yaml").read_text())
    assert cfg["review"]["max_proposals"] <= 5 and cfg["heavy"]["disable_thinking"] is True
    tim = (ROOT / "ops" / "systemd" / "a9-review.timer").read_text()
    assert "Sat" in tim and "09:00" in tim
    wd = yaml.safe_load((ROOT / "config" / "watchdog.yaml").read_text())
    assert "a9-review" in wd["timers"] and wd["heartbeats"]["review"]["unit"] == "a9-review"
    src = (ROOT / "src" / "a9_review" / "service.py").read_text()
    assert "UPDATE journal.control" not in src and "config/" not in src.replace("config_version", ""), "A9 never applies changes"
