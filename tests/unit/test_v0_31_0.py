"""v0.31.0 (2026-11-07): trend day mode. Pure rules plus wiring."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from common import trendday as td
from c3_gate.rules import GateVerdict, trend_day_long_filter

ROOT = Path(__file__).resolve().parents[2]
RAW = yaml.safe_load((ROOT / "config" / "trend_day.yaml").read_text())["trend_day"]
CFG = {**RAW, "enabled": True}      # v0.31.6 switched the mode off in config; the rules are still tested as built
ET = ZoneInfo("America/New_York")


def test_config():
    assert RAW["enabled"] is False and CFG["min_drop_pct"] == 0.02 and CFG["direction"] == "down"
    assert CFG["symbols"] == ["QQQ", "SPY"] and CFG["check_from_et"] == "10:00"
    assert CFG["scanner"]["max_concurrent_positions"] == 4 and CFG["shorting"]["max_gross_short_notional_pct"] == 0.60


def test_detect_thursday_and_friday():
    # QQQ 2026-11-05: prev 691.95, 09:45 671.22, 10:00 666.64 -> down 3.7% and below 09:45
    assert td.detect(691.95, 671.22, 666.64, CFG) is True
    # QQQ 2026-11-06 (bounce): prev 647.05, 09:45 662.63, 10:00 662.85 -> up: never
    assert td.detect(647.05, 662.63, 662.85, CFG) is False
    # down 2.5% but bouncing above the 09:45 price: no
    assert td.detect(100.0, 96.0, 97.5, CFG) is False
    # down only 1.5%: no
    assert td.detect(100.0, 99.0, 98.5, CFG) is False
    # missing inputs never trigger; disabled never triggers
    assert td.detect(None, 96.0, 95.0, CFG) is False
    assert td.detect(100.0, None, 95.0, CFG) is False
    assert td.detect(100.0, 96.0, 95.0, {**CFG, "enabled": False}) is False
    assert td.detect(100.0, None, 95.0, {**CFG, "require_below_0945": False}) is True


def test_flag_roundtrip_and_time():
    v = td.flag_value("2026-11-05")
    assert v == "down@2026-11-05"
    assert td.parse_flag(v, "2026-11-05") == "down"
    assert td.parse_flag(v, "2026-11-06") is None          # stale flag is inert
    assert td.parse_flag("0", "2026-11-05") is None and td.parse_flag(None, "x") is None
    assert td.past_check_time(datetime(2026, 11, 5, 10, 0, tzinfo=ET), CFG)
    assert not td.past_check_time(datetime(2026, 11, 5, 9, 59, tzinfo=ET), CFG)


def test_caps_and_overrides():
    sc = {"max_per_scan": 2, "max_per_hour": 6, "max_concurrent_positions": 2, "min_emit_score": 0.6}
    out = td.scanner_caps(sc, CFG)
    assert (out["max_per_scan"], out["max_per_hour"], out["max_concurrent_positions"]) == (4, 12, 4)
    assert out["min_emit_score"] == 0.6 and sc["max_per_scan"] == 2
    g = {"require_above_vwap": True, "range30_min_pos": 0.5, "stale_max_min": 6}
    assert td.gate_scanner_cfg(g, "down", CFG) == {"require_above_vwap": False, "range30_min_pos": 0.0, "stale_max_min": 6}
    assert td.gate_scanner_cfg(g, "up", CFG) is g
    cap, scn, sh = td.risk_overrides({"heat_split": {"SHORT": 0.02, "LONG": 0.01}, "max_portfolio_heat_pct": 0.03},
                                     {"max_concurrent_positions": 2, "max_position_notional_pct": 0.30},
                                     {"mode": "live", "max_gross_short_notional_pct": 0.30, "max_short_heat_pct": 0.015}, CFG)
    assert cap["heat_split"] == {"SHORT": 0.03, "LONG": 0.01} and cap["max_portfolio_heat_pct"] == 0.04
    assert scn == {"max_concurrent_positions": 4, "max_position_notional_pct": 0.15}
    assert sh["max_gross_short_notional_pct"] == 0.60 and sh["max_short_heat_pct"] == 0.03 and sh["mode"] == "live"


def test_long_filter():
    p = GateVerdict("PASS", "intraday", None, {"pct_move": 0.02})
    v = trend_day_long_filter(p, {"direction": "up"}, "down", CFG)
    assert v.verdict == "VETO" and v.veto_reason == "TREND_DAY_LONG" and v.numbers["trend_day"] == "down"
    assert trend_day_long_filter(p, {"direction": "down"}, "down", CFG) is p
    assert trend_day_long_filter(p, {"direction": "up"}, None, CFG) is p
    assert trend_day_long_filter(p, {"direction": "up"}, "down", {"block_new_longs": False}) is p
    veto = GateVerdict("VETO", "intraday", "GATE_NO_CONFIRM", {})
    assert trend_day_long_filter(veto, {"direction": "up"}, "down", CFG) is veto


def test_wiring():
    c10 = (ROOT / "src" / "c10_scanner" / "service.py").read_text()
    assert "trendday.evaluate_and_set(self.md, self.now_fn(), self.tcfg" in c10 and "dcfg = caps_cfg if" in c10
    c3 = (ROOT / "src" / "c3_gate" / "service.py").read_text()
    assert c3.count("trend_day_long_filter(verdict, thesis, self._trend, self.tcfg)") == 2
    assert "trendday.gate_scanner_cfg(scfg" in c3 and 'lane == "news_short" and self._trend == "down"' in c3
    a3 = (ROOT / "src" / "a3_risk" / "service.py").read_text()
    assert "trendday.risk_overrides(" in a3 and 'veto_reason="TREND_DAY_LONG"' in a3
    assert "shorting_cfg=shorting_eff" in a3 and "scanner_capital_cfg(capital_eff, scanner_eff)" in a3
    assert 'scanner_eff.get("max_concurrent_positions", 2)' in a3
