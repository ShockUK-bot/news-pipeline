"""v0.27.0 (2026-11-02): stop the bleeding. Thesis entries paused, news shorts
off, scalp exits without the time stop or the scale-out and a 3.0 ATR trail,
scanner session from 10:00 ET with a stop-out cool-off, news longs EDGAR only
at half risk. Evidence: docs/claude_october-loss-review-2026-11-02.md."""
from pathlib import Path

import yaml

from a3_risk.sizing import news_capital_cfg
from c10_scanner.rules import cooloff_calendar_days
from c3_gate.rules import GateVerdict, lane_source_filter
from c4_exec.exits import evaluate_on_bar

ROOT = Path(__file__).resolve().parents[2]


def _cfg(name):
    return yaml.safe_load((ROOT / "config" / name).read_text())


def test_config_switches():
    assert _cfg("thesis_entry.yaml")["entry"]["max_new_per_day"] == 0
    assert _cfg("shorting.yaml")["shorting"]["lanes"]["news_short"] is False
    assert _cfg("shorting.yaml")["shorting"]["lanes"]["scanner"] is True
    scalp = _cfg("exit_profiles.yaml")["profiles"]["scalp_v1"]
    assert scalp["time_stop"] is None
    assert scalp["trail"]["k"] == 3.0 and scalp["trail"]["activate_at_R"] == 1.0
    assert scalp["realization"]["action"] == "none"
    assert scalp["realization"]["target_fraction"] == 0.6      # A11 still journals the target
    sc = _cfg("scanner.yaml")["scanner"]
    assert sc["session_start_et"] == "09:33" and sc["stop_out_cooloff_sessions"] == 3  # v0.31.1: A9 proposal 3
    assert sc["early_window"]["enabled"] is True                # shadow window untouched
    assert _cfg("gate.yaml")["gate"]["news_long_sources"] == ["edgar"]
    assert _cfg("risk.yaml")["news"]["long_risk_multiplier"] == 0.5


def _scalp_pos(action="none"):
    policy = {"profile": "scalp_v1",
              "initial_stop": {"method": "atr_5m", "k": 2.0, "price": 98.0},
              "breakeven_at_R": 0.75,
              "trail": {"activate_at_R": 1.0, "method": "atr_5m", "k": 3.0},
              "time_stop": None,
              "realization": {"target_fraction": 0.6, "action": action},
              "magnitude_est": 0.03, "atr_14": 1.0, "atr_value": 1.0}
    return {"position_id": 1, "ticker": "ACME", "horizon": "SHORT", "side": "LONG",
            "qty_open": 100, "avg_entry": 100.0, "r_unit": 2.0,
            "exit_policy": policy, "opened_ts": None, "last_price": None}


def _bar(o, h, l, c):
    return {"ts": 1, "open": o, "high": h, "low": l, "close": c}


def test_realization_none_does_not_scale_out_and_no_time_stop():
    # target = 100 * (1 + 0.03 * 0.6) = 101.8; bar touches it
    acts = evaluate_on_bar(_scalp_pos("none"), _bar(101, 102.0, 100.8, 101.5), 0,
                           minutes_open=200)
    assert not any(a.kind == "SCALE_OUT" for a in acts)
    assert not any(a.layer == "TIME" for a in acts)
    # the old behaviour still works for profiles that keep it
    acts = evaluate_on_bar(_scalp_pos("scale_out_50"), _bar(101, 102.0, 100.8, 101.5), 0,
                           minutes_open=200)
    assert any(a.kind == "SCALE_OUT" and a.qty == 50 for a in acts)


def test_lane_source_filter():
    cfg = {"news_long_sources": ["edgar"]}
    p = GateVerdict("PASS", "open_handoff", None, {"pct_move": 0.02})
    up = {"direction": "up"}
    v = lane_source_filter(p, up, "alpaca_benzinga", cfg)
    assert v.verdict == "VETO" and v.veto_reason == "LANE_SOURCE"
    assert v.numbers["source"] == "alpaca_benzinga" and v.numbers["pct_move"] == 0.02
    assert lane_source_filter(p, up, "edgar", cfg).verdict == "PASS"
    assert lane_source_filter(p, up, "EDGAR", cfg).verdict == "PASS"
    assert lane_source_filter(p, {"direction": "down"}, "alpaca_benzinga", cfg).verdict == "PASS"
    assert lane_source_filter(p, up, "alpaca_benzinga", {}).verdict == "PASS"
    veto = GateVerdict("VETO", "intraday", "GATE_NO_CONFIRM", {})
    assert lane_source_filter(veto, up, "alpaca_benzinga", cfg) is veto


def test_cooloff_days():
    assert cooloff_calendar_days(0) == 0
    assert cooloff_calendar_days(3) == 5
    assert cooloff_calendar_days(5) == 7
    assert cooloff_calendar_days(6) == 10


def test_news_capital_cfg():
    cap = {"risk_per_trade_pct": 0.005, "max_position_notional_pct": 0.15}
    assert news_capital_cfg(cap, {"long_risk_multiplier": 0.5}, "LONG")["risk_per_trade_pct"] == 0.0025
    assert news_capital_cfg(cap, {"long_risk_multiplier": 0.5}, "SHORT")["risk_per_trade_pct"] == 0.005
    assert news_capital_cfg(cap, None, "LONG")["risk_per_trade_pct"] == 0.005
    assert cap["risk_per_trade_pct"] == 0.005                   # input untouched


def test_wiring():
    gate = (ROOT / "src" / "c3_gate" / "service.py").read_text()
    assert "SELECT published_ts, source, headline FROM news.news_items" in gate  # v0.29.0 added headline
    assert "lane_source_filter(verdict, thesis, item_source, self.cfg)" in gate
    scan = (ROOT / "src" / "c10_scanner" / "service.py").read_text()
    assert "_cooloff_tickers()" in scan and '"TICKER_COOLOFF"' in scan
    risk = (ROOT / "src" / "a3_risk" / "service.py").read_text()
    assert 'news_capital_cfg(capital_eff, self.news_cfg, side,' in risk  # v0.29.0 lane=, v0.31.0 capital_eff
