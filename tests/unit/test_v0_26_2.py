"""v0.26.2: promotion keeps the entry trail; swing profile profit lock; in-memory policy repair."""
from pathlib import Path

import yaml

from c4_exec.engine import effective_policy, promoted_policy

ROOT = Path(__file__).resolve().parents[2]
PROFILES = yaml.safe_load((ROOT / "config" / "exit_profiles.yaml").read_text())["profiles"]
SCALP = {"profile": "scalp_v1", "initial_stop": {"method": "atr_5m", "k": 2.0, "price": 114.8},
         "trail": {"activate_at_R": 1.0, "method": "atr_5m", "k": 1.5}, "breakeven_at_R": 0.75,
         "time_stop": {"window_minutes": 60, "min_progress_R": 0.5}, "overnight_hold": "force_flat",
         "force_flat_time_et": "15:50", "atr_value": 0.785, "atr_14": 6.9144, "atr_method": "atr_5m_est",
         "current_stop": 118.59, "stop_basis": "trail", "hwm": 126.74}


def test_promotion_keeps_trail_and_5m_atr():
    new = promoted_policy(SCALP, PROFILES["short_term_v1"], 1, "t")
    assert new["profile"] == "short_term_v1" and new["overnight_hold"] == "eod_rule_v1"
    assert new["trail"] == SCALP["trail"] and new["breakeven_at_R"] == 0.75
    assert new["atr_value"] == 0.785 and new["atr_method"] == "atr_5m_est"       # not swapped to daily
    assert new["current_stop"] == 118.59 and "force_flat_time_et" not in new
    # the old behaviour is one flag away
    old = promoted_policy(SCALP, {**PROFILES["short_term_v1"], "promotion_keeps_trail": False}, 1, "t")
    assert old["trail"]["k"] == 2.5 and old["atr_value"] == 6.9144


def test_effective_policy_repairs_a_pre_v0262_promotion():
    hood = {**SCALP, "profile": "short_term_v1", "promoted_from": "scalp_v1",
            "trail": {"k": 2.5, "method": "atr", "activate_at_R": 1.5}, "breakeven_at_R": 1.0,
            "atr_value": 6.9144, "atr_method": "atr"}
    eff = effective_policy(hood, PROFILES, r_unit=1.57)
    assert eff["trail"]["k"] == 1.5 and eff["atr_value"] == 0.785 and eff["atr_method"] == "atr_5m_recovered"
    assert eff["profit_lock"] == {"activate_pct": 0.05, "trail_pct": 0.04}
    assert eff["current_stop"] == 118.59                      # journaled state untouched
    # a plain news position on the swing profile only gains the lock
    news = {"profile": "short_term_v1", "trail": {"k": 2.5, "method": "atr", "activate_at_R": 1.5},
            "atr_value": 3.0, "atr_method": "atr", "initial_stop": {"k": 2.0}}
    eff = effective_policy(news, PROFILES, r_unit=6.0)
    assert eff["trail"]["k"] == 2.5 and eff["profit_lock"]["activate_pct"] == 0.05
    # unknown profile: unchanged
    assert effective_policy({"profile": "x"}, PROFILES, 1.0) == {"profile": "x"}


def test_profile_pins():
    st = PROFILES["short_term_v1"]
    assert st["profit_lock"] == {"activate_pct": 0.05, "trail_pct": 0.04} and st["promotion_keeps_trail"] is True
    assert "profit_lock" not in PROFILES["scalp_v1"]
