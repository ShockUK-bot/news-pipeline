"""v0.12.20 unit tests — the position-5 ARM FAILED bug (2026-07-24).

Every arm of position 5 journaled `ARM FAILED: MIPError('UNRESOLVABLE_REF:
prenews_price')`: the stdlib close_below_prenews predicate resolves
ctx.prenews_price from exit_policy, but A3's materialize_exit_policy never
wrote it — integration tests seeded policy["prenews_price"] directly,
masking the gap. v0.12.20 threads it gate-snapshot -> exit_policy.
"""
from a3_risk.service import A3Service, RiskAdjustments
from common.invalidation_dsl import ArmContext, MIPError, compile_predicate

import pytest


PROFILE = {
    "initial_stop": {"method": "atr", "k": 2.0},
    "catastrophe": {"method": "atr", "k": 3.5},
    "breakeven_at_R": 1.0,
    "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
    "time_stop": {"window": "thesis", "min_progress_R": 0.5},
    "realization": {"target_fraction": 0.7, "action": "scale_out_50"},
    "earnings_blackout_exit": True,
    "overnight_hold": "eod_rule_v1",
}

THESIS = {
    "invalidation": {"machine_checkable": ["close_below_prenews"],
                     "news_checkable": []},
    "magnitude_est": 0.04,
}

ADJ = RiskAdjustments(k=2.0, realization_fraction=0.7,
                      time_window_sessions=3, reason="test")


def _policy(**kw):
    return A3Service.materialize_exit_policy(
        None, "short_term_v1", PROFILE, ADJ, limit_price=100.0,
        atr=2.5, thesis=THESIS, atr_14=2.5, **kw)


# ---- exit_policy carries the reference --------------------------------------

def test_policy_carries_prenews_price():
    policy = _policy(prenews_price=97.42)
    assert policy["prenews_price"] == 97.42


def test_policy_omits_key_when_unavailable():
    # Degrade like before the fix: only the one arm fails, nothing new breaks.
    policy = _policy()
    assert "prenews_price" not in policy


def test_existing_policy_fields_unchanged():
    policy = _policy(prenews_price=97.42)
    assert policy["initial_stop"]["price"] == 95.0        # 100 - 2.0*2.5
    assert policy["machine_invalidations"] == ["close_below_prenews"]
    assert policy["atr_14"] == 2.5


# ---- the arm actually resolves ----------------------------------------------

def _arm_ctx(prenews):
    return ArmContext(entry_price=100.0, initial_stop=95.0, r_unit=5.0,
                      prenews_price=prenews, atr_14=2.5, mark=100.0)


def test_close_below_prenews_arms_with_reference():
    p = compile_predicate({"std": "close_below_prenews"}, _arm_ctx(97.42))
    assert "97.42" in str(p.compiled_form)


def test_close_below_prenews_still_unresolvable_without_reference():
    # Pin the failure mode the fix eliminates on the live path.
    with pytest.raises(MIPError):
        compile_predicate({"std": "close_below_prenews"}, _arm_ctx(None))
