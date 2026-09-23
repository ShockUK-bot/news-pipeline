"""v0.26.0: percent profit lock in the exit ladder; price-confirmed thesis re-entry."""
import sys
from pathlib import Path

import yaml

from c4_exec.exits import evaluate_on_bar
from c11_thesis.service import reentry_verdict

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_exit_engine import bar, make_pos  # noqa: E402

LOCK = {"activate_pct": 0.08, "trail_pct": 0.05}


def test_profit_lock_arms_at_8pct_and_trails_5pct_long():
    # thesis-like policy: 1R = 20% of price, so +10% is 0.5R and the R ladder is silent
    pol = {"profile": "thesis_v1", "atr_14": 6.6667, "initial_stop": {"price": 80.0},
           "breakeven_at_R": 1.0, "trail": {"activate_at_R": 2.0, "method": "atr_weekly", "k": 4.0},
           "current_stop": 80.0, "stop_basis": "initial", "hwm": 100.0, "profit_lock": LOCK}
    pos = make_pos(policy=pol, r_unit=20.0)
    a = evaluate_on_bar(pos, bar(o=105, h=107, l=104, c=106), 0)            # +7%: not yet
    assert not any(x.kind == "SET_STOP" for x in a)
    a = evaluate_on_bar(pos, bar(o=108, h=110, l=107, c=109), 0)            # hwm 110: +10%
    st = [x for x in a if x.kind == "SET_STOP"]
    assert st and st[0].new_stop == 104.5 and st[0].new_basis == "trail" and "profit lock" in st[0].reason
    # without the lock the same bar does nothing (0.5R)
    pos2 = make_pos(policy={**pol, "profit_lock": None}, r_unit=20.0)
    assert not any(x.kind == "SET_STOP" for x in evaluate_on_bar(pos2, bar(o=108, h=110, l=107, c=109), 0))


def test_profit_lock_short_and_tighter_of_two():
    pol = {"profile": "thesis_v1", "side": "SHORT", "atr_14": 6.6667, "initial_stop": {"price": 120.0},
           "breakeven_at_R": 1.0, "trail": {"activate_at_R": 2.0, "method": "atr_weekly", "k": 4.0},
           "current_stop": 120.0, "stop_basis": "initial", "hwm": 100.0, "profit_lock": LOCK}
    pos = make_pos(policy=pol, r_unit=20.0, side="SHORT")
    a = evaluate_on_bar(pos, bar(o=92, h=93, l=88, c=90), 0)                # low 88: +12% for a short
    st = [x for x in a if x.kind == "SET_STOP"]
    assert st and st[0].new_stop == 92.4                                       # 88 * 1.05
    # when the R trail is tighter, it wins: 1R = 2 here, hwm 112 -> R trail 112 - 2.5*2 = 107 vs lock 106.4
    pol2 = {"profile": "short_term_v1", "atr_14": 2.0, "initial_stop": {"price": 96.0},
            "breakeven_at_R": 1.0, "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
            "current_stop": 96.0, "stop_basis": "initial", "hwm": 100.0, "profit_lock": LOCK}
    pos3 = make_pos(policy=pol2, r_unit=4.0)
    a = evaluate_on_bar(pos3, bar(o=110, h=112, l=109, c=111), 0)
    st = [x for x in a if x.kind == "SET_STOP"]
    assert st and st[0].new_stop == 107.0 and "profit lock" not in st[0].reason


def test_reentry_verdict():
    levels = {"INVX": 29.71}
    ok, n = reentry_verdict("INVX", 29.17, levels)
    assert ok is False and n["reentry_need"] == 29.71 and n["last_close"] == 29.17
    assert reentry_verdict("INVX", 29.90, levels)[0] is True
    assert reentry_verdict("INVX", 29.90, levels, buffer_pct=0.02)[0] is False   # needs 30.30
    assert reentry_verdict("RIOT", 21.0, levels) == (True, {})
    assert reentry_verdict("INVX", None, levels)[0] is True


def test_config_pins_and_wiring():
    prof = yaml.safe_load((ROOT / "config" / "exit_profiles.yaml").read_text())["profiles"]
    for p in ("thesis_v1", "long_term_v1"):
        assert prof[p]["profit_lock"] == {"activate_pct": 0.08, "trail_pct": 0.05}
    assert "profit_lock" not in prof["scalp_v1"] and "profit_lock" not in prof["short_term_v1"]
    t = yaml.safe_load((ROOT / "config" / "thesis_entry.yaml").read_text())["entry"]
    assert t["reentry"]["enabled"] is True and t["reentry"]["lookback_days"] == 30 and t["reentry_cooloff_days"] == 1
    svc = (ROOT / "src" / "c4_exec" / "service.py").read_text()
    assert "engine.profiles = exit_cfg.get" in svc
    c11 = (ROOT / "src" / "c11_thesis" / "service.py").read_text()
    assert 'skip_reason = "REENTRY_WAIT"' in c11
