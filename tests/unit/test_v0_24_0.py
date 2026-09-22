"""v0.24.0: drift short shadow, widened fade trigger, scanner early window shadow, A9 rules."""
import sys
from pathlib import Path

import yaml

from c3_gate.rules import GateVerdict, drift_candidate, fade_candidate
from c10_scanner.rules import emission_disposition, in_early_window, in_scan_window
from a9_review.candidates import generate, rule_drift_short, rule_fade_lane, rule_scanner_early_window

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v0_14_12 import CFG as GCFG, state, thesis, veto  # noqa: E402
from test_scanner import CFG as SCFG  # noqa: E402

CFG = {**GCFG, "fade": {"enabled": True, "min_move_pct": 0.01, "max_move_pct": 0.12, "window_max_min_after_open": 180,
                        "source_vetoes": ["PRICED_IN", "GATE_EXTENDED", "HANDOFF_UNMOVED", "GATE_NO_CONFIRM"]},
       "drift": {"enabled": True, "source_vetoes": ["PRICED_IN", "GATE_EXTENDED"], "min_drop_pct": 0.01,
                 "max_drop_pct": 0.15, "window_max_min_after_open": 240}}


def test_fade_widened():
    assert fade_candidate(thesis(), state(last=101.5, mso=30), veto("GATE_NO_CONFIRM"), CFG)["pct_move"] == 0.015
    assert fade_candidate(thesis(), state(last=103.0, mso=150), veto("HANDOFF_UNMOVED"), CFG) is not None
    assert fade_candidate(thesis(), state(last=103.0, mso=200), veto("PRICED_IN"), CFG) is None      # past 180 min
    assert fade_candidate(thesis(), state(last=100.5, mso=30), veto("PRICED_IN"), CFG) is None       # under 1%
    assert fade_candidate(thesis(), state(last=103.0, mso=30), veto("CREDIBILITY"), CFG) is None     # not a source
    # the old config still behaves as before
    assert fade_candidate(thesis(), state(last=103.0, mso=30), veto("PRICED_IN"), GCFG) is None


def test_drift_candidate():
    d = drift_candidate(thesis("down"), state(last=95.0, mso=40), veto("PRICED_IN"), CFG)
    assert d is not None and d["drop_pct"] == 0.05 and d["pct_move"] == -0.05 and d["source_veto"] == "PRICED_IN"
    assert drift_candidate(thesis("down"), state(last=95.0, mso=40), veto("GATE_EXTENDED", "intraday"), CFG)["source_rule"] == "intraday"
    assert drift_candidate(thesis("up"), state(last=95.0, mso=40), veto("PRICED_IN"), CFG) is None       # bullish: not drift
    assert drift_candidate(thesis("down"), state(last=99.5, mso=40), veto("PRICED_IN"), CFG) is None     # under 1%
    assert drift_candidate(thesis("down"), state(last=80.0, mso=40), veto("PRICED_IN"), CFG) is None     # 20%: capitulation
    assert drift_candidate(thesis("down"), state(last=95.0, mso=10), veto("PRICED_IN"), CFG) is None     # blackout
    assert drift_candidate(thesis("down"), state(last=95.0, mso=300), veto("PRICED_IN"), CFG) is None    # past window
    assert drift_candidate(thesis("down"), state(last=95.0, mso=40), veto("GATE_NO_CONFIRM"), CFG) is None
    assert drift_candidate(thesis("down"), state(last=95.0, mso=40), GateVerdict("PASS", "intraday", None, {}), CFG) is None
    off = {**CFG, "drift": {"enabled": False}}
    assert drift_candidate(thesis("down"), state(last=95.0, mso=40), veto("PRICED_IN"), off) is None


def test_scanner_early_window():
    cfg = {**SCFG, "early_window": {"enabled": True, "start_et": "09:33"}}
    assert in_scan_window("09:33", cfg) and in_early_window("09:33", cfg) and in_early_window("09:49", cfg)
    assert not in_early_window("09:50", cfg) and in_scan_window("09:50", cfg)
    assert not in_scan_window("09:32", cfg) and not in_early_window("09:32", cfg)
    assert not in_early_window("09:40", SCFG) and not in_scan_window("09:40", SCFG)   # disabled: old behaviour


def test_early_window_disposition_journals_never_emits():
    from test_scanner import metrics  # noqa: E402
    m = metrics()
    cfg = {**SCFG, "max_per_scan": 2, "max_per_day": 15, "max_per_hour": 6, "max_concurrent_positions": 2,
           "min_emit_score": 0.6, "precheck_short_availability": False}
    kw = dict(emitted_this_scan=0, emitted_today=0, emitted_last_hour=0, open_scanner=0)
    assert emission_disposition(0.9, m, cfg, **kw) is None
    assert emission_disposition(0.9, m, cfg, early=True, **kw) == ("CAPPED", "EARLY_WINDOW")
    assert emission_disposition(0.1, m, cfg, early=True, **kw) == ("FILTERED", "SCORE_FLOOR")   # floor still first


def test_a9_rules_v0_24():
    quiet = {"shadow_lanes": {"drift_short": {"n": 12, "avg_eod_pct": 0.9, "median_eod_pct": 0.3, "win_pct": 70, "sessions": 5, "neg_sessions": 1},
                              "fade": {"n": 3}},
             "funnel": {"by_outcome": {"CAPPED_EARLY_WINDOW": {"n": 6, "with_move_r": 2.0, "with_move_winners": 4}}}}
    p, w = rule_drift_short(quiet); assert p is None and "12 of 60" in w
    p, w = rule_fade_lane(quiet); assert p is None and "3 of 60" in w
    p, w = rule_scanner_early_window(quiet); assert p is None and "6 of 20" in w
    hot = {"shadow_lanes": {"drift_short": {"n": 80, "avg_eod_pct": 0.9, "median_eod_pct": 0.3, "win_pct": 62, "sessions": 20, "neg_sessions": 6},
                            "fade": {"n": 70, "avg_eod_pct": 0.2, "median_eod_pct": 0.1, "win_pct": 58, "sessions": 20, "neg_sessions": 8}},
           "funnel": {"by_outcome": {"CAPPED_EARLY_WINDOW": {"n": 25, "with_move_r": 6.5, "with_move_winners": 15}}}}
    p, _ = rule_drift_short(hot); assert p and "drift short lane" in p["title"] and p["evidence"]["n_instances"] == 80
    p, w = rule_fade_lane(hot); assert p is None and "under the bar" in w         # avg after cost 0.1 < 0.4
    p, _ = rule_scanner_early_window(hot); assert p and "09:33" in p["title"]
    props, _ = generate(hot, max_proposals=5)
    assert {x["rule"] for x in props} >= {"rule_drift_short", "rule_scanner_early_window"}


def test_yaml_pins():
    g = yaml.safe_load((ROOT / "config" / "gate.yaml").read_text())["gate"] if "gate" in yaml.safe_load((ROOT / "config" / "gate.yaml").read_text()) else yaml.safe_load((ROOT / "config" / "gate.yaml").read_text())
    f = g["fade"]; d = g["drift"]
    assert f["min_move_pct"] == 0.01 and f["window_max_min_after_open"] == 180 and "GATE_NO_CONFIRM" in f["source_vetoes"]
    assert d["enabled"] is True and d["source_vetoes"] == ["PRICED_IN", "GATE_EXTENDED"]
    s = yaml.safe_load((ROOT / "config" / "scanner.yaml").read_text())["scanner"]
    assert s["early_window"]["enabled"] is True and s["early_window"]["start_et"] == "09:33" and s["session_start_et"] == "09:50"
    svc = (ROOT / "src" / "c3_gate" / "service.py").read_text()
    assert "await self._drift_shadow(" in svc and "rule='drift_short'" in svc.replace('"', "'")
