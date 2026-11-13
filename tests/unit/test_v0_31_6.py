"""v0.31.6 (2026-11-13): four switches after the review challenge. Dilution
shorts off, scanner start back to 10:00 ET, trend day mode off, and the news
LONG lane in shadow (GATE VETO LANE_SHADOW with a counterfactual, no order)."""
from pathlib import Path

import yaml

from c3_gate.rules import GateVerdict, lane_source_filter
from c10_scanner.rules import in_early_window, in_scan_window
from common import trendday as td

ROOT = Path(__file__).resolve().parents[2]


def _cfg(name):
    return yaml.safe_load((ROOT / "config" / name).read_text())


PASS = GateVerdict("PASS", "intraday", None, {"pct_move": 0.03})
UP, DOWN = {"direction": "up"}, {"direction": "down"}


def test_config_switches():
    assert _cfg("shorting.yaml")["shorting"]["lanes"]["news_short_dilution"] is False
    assert _cfg("shorting.yaml")["shorting"]["lanes"]["news_short"] is False
    assert _cfg("shorting.yaml")["shorting"]["mode"] == "live"          # scanner shorts stay live
    assert _cfg("trend_day.yaml")["trend_day"]["enabled"] is False
    g = _cfg("gate.yaml")["gate"]
    assert g["news_long_shadow"] is True and g["news_long_sources"] == ["edgar"]
    sc = _cfg("scanner.yaml")["scanner"]
    assert sc["session_start_et"] == "10:00" and sc["early_window"]["enabled"] is True


def test_news_long_shadow_vetoes_the_longs_that_would_have_traded():
    cfg = {"news_long_sources": ["edgar"], "news_long_shadow": True}
    v = lane_source_filter(PASS, UP, "edgar", cfg)
    assert v.verdict == "VETO" and v.veto_reason == "LANE_SHADOW" and v.rule == "intraday"
    assert v.numbers["shadow"] is True and v.numbers["source"] == "edgar" and v.numbers["pct_move"] == 0.03
    assert PASS.numbers == {"pct_move": 0.03}                           # input untouched
    # other sources keep the reason they had, so the two samples stay separate
    assert lane_source_filter(PASS, UP, "alpaca_benzinga", cfg).veto_reason == "LANE_SOURCE"
    assert lane_source_filter(PASS, UP, None, cfg).veto_reason == "LANE_SOURCE"
    # shorts and existing vetoes are untouched
    assert lane_source_filter(PASS, DOWN, "edgar", cfg) is PASS
    veto = GateVerdict("VETO", "intraday", "GATE_NO_CONFIRM", {})
    assert lane_source_filter(veto, UP, "edgar", cfg) is veto


def test_news_long_shadow_without_a_source_list_and_switched_off():
    assert lane_source_filter(PASS, UP, "alpaca_benzinga", {"news_long_shadow": True}).veto_reason == "LANE_SHADOW"
    assert lane_source_filter(PASS, UP, None, {"news_long_shadow": True}).veto_reason == "LANE_SHADOW"
    off = {"news_long_sources": ["edgar"], "news_long_shadow": False}
    assert lane_source_filter(PASS, UP, "edgar", off) is PASS           # the v0.27.0 behaviour
    assert lane_source_filter(PASS, UP, "alpaca_benzinga", off).veto_reason == "LANE_SOURCE"
    assert lane_source_filter(PASS, UP, "alpaca_benzinga", {}) is PASS


def test_scanner_window_with_the_live_config():
    sc = _cfg("scanner.yaml")["scanner"]
    assert in_scan_window("09:45", sc) and in_early_window("09:45", sc)      # scanned, journaled, not emitted
    assert in_early_window("09:59", sc) and not in_early_window("10:00", sc)
    assert in_scan_window("10:00", sc) and not in_scan_window("09:32", sc)


def test_trend_day_off_never_triggers():
    cfg = _cfg("trend_day.yaml")["trend_day"]
    assert td.detect(691.95, 671.22, 666.64, cfg) is False              # 2026-11-05, the day it was built for
    assert td.detect(691.95, 671.22, 666.64, {**cfg, "enabled": True}) is True
