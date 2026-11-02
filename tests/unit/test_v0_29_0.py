"""v0.29.0 (2026-11-02): dilution short lane (live, half risk, ETB only) and
the named exhaustion-fade buckets A9 watches."""
from pathlib import Path

import yaml

from a3_risk.sizing import news_capital_cfg
from a9_review.candidates import generate, rule_fade_down_capped, rule_fade_up_structure
from c3_gate.rules import catalyst_class, credibility_with_class, lane_for

ROOT = Path(__file__).resolve().parents[2]


def _cfg(name):
    return yaml.safe_load((ROOT / "config" / name).read_text())


def test_catalyst_class():
    yes = ["Cadiz Raises $21 Million in Registered Direct Offering",
           "authID Announces Pricing of Up To $1,350,000 Private Placement Offering",
           "Meta Announces Public Offerings of Depositary Shares and Class A Common Stock",
           "Phaos Technology Announces One-For-Fifteen Reverse Stock Split",
           "Global Engine Group Holding Limited Announces 1-for-10 Share Consolidation",
           "XYZ Prices Upsized Underwritten Public Offering of Common Stock",
           "ABC enters securities purchase agreement for $5M",
           "DEF launches $50 million at-the-market equity program",
           "GHI prices $400 million convertible senior notes offering"]
    for h in yes:
        assert catalyst_class(h) == "offering", h
    no = ["Nebius Acquires Inferize; Terms Undisclosed", "Rivian Reports Q3 Production Of 19,751 Vehicles",
          "Visa and Mastercard Face Potential £5bn UK Lawsuit", "8-K - EchoStar CORP (0001415404) (Filer)", None, ""]
    for h in no:
        assert catalyst_class(h) is None, h


def test_lane_for():
    down = {"direction": "down", "horizon": "SHORT"}
    assert lane_for(down, "news", "offering") == "news_short_dilution"
    assert lane_for(down, "news", None) == "news_short"
    assert lane_for({"direction": "up", "horizon": "SHORT"}, "news", "offering") == "news_short"
    assert lane_for({"direction": "up", "horizon": "LONG"}, "news", None) == "news_long"
    assert lane_for(down, "scanner", "offering") == "scanner"


def test_credibility_with_class():
    cfg = {"credibility_exempt_classes": ["offering"]}
    assert credibility_with_class(2, "offering", cfg) == 1
    assert credibility_with_class(3, None, cfg) == 3
    assert credibility_with_class(2, "offering", {}) == 2
    assert credibility_with_class(1, "offering", cfg) == 1


def test_config():
    lanes = _cfg("shorting.yaml")["shorting"]["lanes"]
    assert lanes["news_short"] is False and lanes["news_short_dilution"] is True
    assert _cfg("shorting.yaml")["shorting"]["etb_only"] is True
    assert _cfg("gate.yaml")["gate"]["credibility_exempt_classes"] == ["offering"]
    assert _cfg("risk.yaml")["news"]["short_dilution_risk_multiplier"] == 0.5


def test_news_capital_cfg_lane():
    cap = {"risk_per_trade_pct": 0.005}
    n = {"long_risk_multiplier": 0.5, "short_dilution_risk_multiplier": 0.5}
    assert news_capital_cfg(cap, n, "SHORT", lane="news_short_dilution")["risk_per_trade_pct"] == 0.0025
    assert news_capital_cfg(cap, n, "SHORT", lane="news_short")["risk_per_trade_pct"] == 0.005
    assert news_capital_cfg(cap, n, "SHORT")["risk_per_trade_pct"] == 0.005
    assert news_capital_cfg(cap, n, "LONG", lane="news_long")["risk_per_trade_pct"] == 0.0025


def test_fade_rules():
    quiet = {"funnel": {"fade": {"up_structure": {"n": 12, "sum_r": 3.0, "winners": 8},
                                 "down_capped": {"n": 5, "sum_r": 1.0, "winners": 3}}}}
    p, w = rule_fade_up_structure(quiet); assert p is None and "12 of 30" in w
    p, w = rule_fade_down_capped(quiet); assert p is None and "5 of 30" in w
    hot = {"funnel": {"fade": {"up_structure": {"n": 31, "sum_r": 6.5, "winners": 20},
                               "down_capped": {"n": 40, "sum_r": 9.0, "winners": 26}}}}
    p, _ = rule_fade_up_structure(hot); assert p and p["evidence"]["n_instances"] == 31 and "short" in p["proposed_diff"]
    p, _ = rule_fade_down_capped(hot); assert p and "long" in p["proposed_diff"]
    under = {"funnel": {"fade": {"up_structure": {"n": 31, "sum_r": 6.5, "winners": 10}}}}
    p, w = rule_fade_up_structure(under); assert p is None and "under the bar" in w
    props, _ = generate(hot)
    assert {x["rule"] for x in props} >= {"rule_fade_up_structure", "rule_fade_down_capped"}


def test_wiring():
    svc = (ROOT / "src" / "c3_gate" / "service.py").read_text()
    assert "SELECT published_ts, source, headline FROM news.news_items" in svc
    assert "cclass = catalyst_class(row[2])" in svc and 'lane = self._lane_for(thesis, "news", cclass)' in svc
    assert "evaluate(thesis, state, self.cfg, short, cclass=cclass)" in svc
    assert '"lane": lane, "catalyst_class": cclass' in svc
    risk = (ROOT / "src" / "a3_risk" / "service.py").read_text()
    assert 'lane=gate.get("lane")' in risk
    fun = (ROOT / "src" / "a11_metrics" / "funnel.py").read_text()
    assert '"fade": {"up_structure"' in fun
