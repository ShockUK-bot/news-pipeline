"""v0.23.0: scanner funnel counterfactuals (classification, A9 rules, wiring)."""
from pathlib import Path

from a11_metrics.funnel import classify
from a9_review.candidates import generate, rule_analyst_short_bias, rule_scanner_concurrency

ROOT = Path(__file__).resolve().parents[2]


def test_classify_outcomes():
    assert classify("CAPPED", "CONCURRENT", []) == ("CAPPED_CONCURRENT", None)
    assert classify("CAPPED", "PER_SCAN", []) == ("CAPPED_PER_SCAN", None)
    chain = [("ANALYST", "THESIS", None, "x"), ("GATE", "VETO", "SCANNER_STRUCTURE", "y")]
    assert classify("EMITTED", None, chain) == ("GATE_VETO", "SCANNER_STRUCTURE")
    chain = [("ANALYST", "THESIS", None, "x"), ("GATE", "PASS", None, "y"), ("RISK", "VETO", "SIZE_CLIPPED", "z")]
    assert classify("EMITTED", None, chain) == ("RISK_VETO", "SIZE_CLIPPED")
    assert classify("EMITTED", None, [("ANALYST", "REJECT", None, "no-trade: top of range")])[0] == "ANALYST_REJECT"
    assert classify("EMITTED", None, [("ANALYST", "THESIS", None, "x"), ("GATE", "PASS", None, ""), ("RISK", "SHADOW_SHORT", None, "")])[0] == "SHADOW_SHORT"
    assert classify("EMITTED", None, []) == ("NO_DECISION", None)


def test_a9_funnel_rules():
    quiet = {"funnel": {"by_outcome": {"CAPPED_CONCURRENT": {"n": 4, "with_move_r": 1.0, "with_move_winners": 2}},
                        "analyst_short_on_up_move": {"n": 3, "short_sum_r": -0.5, "long_sum_r": 1.5, "long_better": 2}}}
    p, w = rule_scanner_concurrency(quiet)
    assert p is None and "4 of 10" in w
    p, w = rule_analyst_short_bias(quiet)
    assert p is None and "3 of 8" in w
    hot = {"funnel": {"by_outcome": {"CAPPED_CONCURRENT": {"n": 12, "with_move_r": 5.2, "with_move_winners": 8}},
                      "analyst_short_on_up_move": {"n": 9, "short_sum_r": -2.0, "long_sum_r": 3.5, "long_better": 7}}}
    p, _ = rule_scanner_concurrency(hot)
    assert p and "2 -> 3" in p["proposed_diff"] and p["evidence"]["n_instances"] == 12
    p, _ = rule_analyst_short_bias(hot)
    assert p and "WITH the detected move" in p["title"]
    props, watch = generate(hot)
    assert {x["rule"] for x in props} >= {"rule_scanner_concurrency", "rule_analyst_short_bias"}
    assert generate({})[0] == []


def test_wiring():
    a11 = (ROOT / "src" / "a11_metrics" / "service.py").read_text()
    assert "funnel_pass(conn" in a11 and "--funnel-days" in a11
    a9 = (ROOT / "src" / "a9_review" / "service.py").read_text()
    assert 'ev["funnel"]' in a9
    assert (ROOT / "schema" / "migrations" / "021-scanner-funnel-cf.sql").exists()
