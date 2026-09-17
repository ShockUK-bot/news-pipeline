"""v0.16.0: scanner lane sizing policy (risk multiplier 1.0 + lane notional cap)
and the exit-ladder replay variants used by the nightly counterfactual job."""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from a3_risk.sizing import scanner_capital_cfg, size_entry
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_risk_exec import CAPITAL, LIMITS, PROFILE, inputs  # noqa: E402
sys.path.insert(0, str(ROOT / "ops" / "tools"))
from scalp_replay import VARIANTS, replay, replay_variants  # noqa: E402

CT = ZoneInfo("America/Chicago")


def test_scanner_capital_cfg_applies_multiplier_and_lane_cap():
    cfg = scanner_capital_cfg(CAPITAL, {"risk_multiplier": 1.0, "max_position_notional_pct": 0.25})
    assert cfg["risk_per_trade_pct"] == 0.005 and cfg["max_position_notional_pct"] == 0.25
    assert CAPITAL["max_position_notional_pct"] == 0.15, "input must not be mutated"
    legacy = scanner_capital_cfg(CAPITAL, {"risk_multiplier": 0.5})
    assert legacy["risk_per_trade_pct"] == 0.0025 and legacy["max_position_notional_pct"] == 0.15


def test_lane_cap_lets_risk_sizing_bind_on_a_large_cap_scalp():
    # Stop 1% of price (the live large-cap case): the notional cap binds under
    # both policies, but the lane cap carries 2x the shares and the risk.
    lane = {"risk_multiplier": 1.0, "max_position_notional_pct": 0.30}
    inp = inputs(atr_14=0.5)
    old = size_entry(inp, scanner_capital_cfg(CAPITAL, {"risk_multiplier": 0.5}), LIMITS, PROFILE, "LONG", 2.0)
    new = size_entry(inp, scanner_capital_cfg(CAPITAL, lane), LIMITS, PROFILE, "LONG", 2.0)
    assert old.verdict == "SIZE" and old.numbers["binding_clip"] == "notional" and old.qty == 74
    assert new.verdict == "SIZE" and new.numbers["binding_clip"] == "notional" and new.qty == 149
    assert new.actual_risk > 1.9 * old.actual_risk
    # 0.25 would trip the viability rule on the same trade (clipped risk under
    # half the budget): that is why the lane cap is 0.30.
    r25 = size_entry(inp, scanner_capital_cfg(CAPITAL, {"risk_multiplier": 1.0, "max_position_notional_pct": 0.25}),
                     LIMITS, PROFILE, "LONG", 2.0)
    assert r25.verdict == "VETO" and r25.veto_reason == "SIZE_CLIPPED"
    # Stop 3% of price: risk sizing binds under the lane policy (83 shares,
    # ~250 at risk on 50k), where the old policy sized 41.
    wide = inputs(atr_14=1.5)
    old = size_entry(wide, scanner_capital_cfg(CAPITAL, {"risk_multiplier": 0.5}), LIMITS, PROFILE, "LONG", 2.0)
    new = size_entry(wide, scanner_capital_cfg(CAPITAL, lane), LIMITS, PROFILE, "LONG", 2.0)
    assert old.qty == 41 and new.qty == 83 and new.numbers["binding_clip"] is None
    assert new.risk_budget == 250.0


def test_risk_yaml_pins():
    r = yaml.safe_load((ROOT / "config" / "risk.yaml").read_text())
    assert r["scanner"]["risk_multiplier"] == 1.0
    assert r["scanner"]["max_position_notional_pct"] == 0.30
    assert r["scanner"]["max_concurrent_positions"] == 2
    assert r["capital"]["max_position_notional_pct"] == 0.15, "global cap unchanged"


def bars(path, start="08:52"):
    """path: list of closes, one per minute from start; high/low = close +/- 0.05."""
    t = datetime(2026, 9, 17, int(start[:2]), int(start[3:]), tzinfo=CT)
    return [{"ts": t + timedelta(minutes=i), "open": c, "high": c + 0.05, "low": c - 0.05, "close": c}
            for i, c in enumerate(path)]


def test_variants_differ_only_where_they_should():
    # runs to +3R then gives it all back: scale-out banks half at the target,
    # noscale rides the full size into the trail, runner_hold rides to breakeven
    path = [100 + 0.1 * i for i in range(30)] + [103 - 0.1 * i for i in range(35)] + [99.5] * 300
    b = bars(path)
    entry = b[0]["ts"]
    res = replay_variants(b, "LONG", entry, 100.0, 100, 0.5, 0.015)
    assert set(res) == set(VARIANTS)
    base, noscale, hold = res["base"], res["noscale"], res["runner_hold"]
    assert [e[1] for e in base["exits"]] == ["TARGET", "TRAIL"]
    assert [e[1] for e in noscale["exits"]] == ["TRAIL"] and noscale["pnl"] > base["pnl"]
    assert [e[1] for e in hold["exits"]] == ["TARGET", "BREAKEVEN"] and hold["pnl"] < base["pnl"]
    assert base["mfe_r"] > 2.5 and base["close_r"] is not None and base["close_r"] < 0


def test_time_stop_and_stop_width_variants():
    # drifts +0.3R for 90 minutes: the time stop closes it at 60 min, notime holds
    path = [100 + 0.001 * i for i in range(400)]
    b = bars(path)
    res = replay_variants(b, "LONG", b[0]["ts"], 100.0, 100, 0.5, 0.015)
    assert res["base"]["exits"][0][1] == "TIME" and res["notime"]["exits"][0][1] == "FORCE_FLAT"
    assert res["stop3.0"]["r_unit"] == 1.5 and res["base"]["r_unit"] == 1.0
    # legacy positional call still works (CLI compatibility)
    r = replay(b, "LONG", b[0]["ts"], 100.0, 100, 0.5, 0.015, 2.0)
    assert r["exits"][0][1] == "TIME"


def test_units_and_watchdog_wired():
    svc = (ROOT / "ops" / "systemd" / "scanner-counterfactual.service").read_text()
    tim = (ROOT / "ops" / "systemd" / "scanner-counterfactual.timer").read_text()
    assert "scanner_counterfactuals.py" in svc and "Type=oneshot" in svc
    assert "15:05" in tim
    wd = yaml.safe_load((ROOT / "config" / "watchdog.yaml").read_text())
    assert "scanner-counterfactual" in wd["timers"]
    assert "017-scanner-counterfactuals.sql" in {p.name for p in (ROOT / "schema" / "migrations").iterdir()}
