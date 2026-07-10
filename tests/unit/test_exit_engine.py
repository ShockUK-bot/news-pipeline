"""Phase 4 chunk-2 unit tests: exit evaluator (layer priority, stop
attribution, tighten-only ratchets, HWM, scale-out), D1 overnight matrix.
Pure functions, no DB."""
import pytest

from c4_exec.exits import (ExitAction, evaluate_on_bar, policy_state,
                           realization_target)
from c4_exec.overnight import overnight_decision, realized_move_fraction

ON_CFG = {"hold_min_unrealized_R": 0.3, "young_max_age_sessions": 1,
          "young_max_realized_fraction": 0.5}


def make_pos(**over):
    policy = {
        "profile": "short_term_v1",
        "initial_stop": {"method": "atr", "k": 2.0, "price": 96.0},
        "catastrophe_stop_broker": {"k": 3.5, "price": 93.0},
        "breakeven_at_R": 1.0,
        "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
        "time_stop": {"window": "2_sessions", "min_progress_R": 0.5},
        "realization": {"target_fraction": 0.7, "action": "scale_out_50"},
        "magnitude_est": 0.055,
        "atr_14": 2.0,
    }
    policy.update(over.pop("policy", {}))
    pos = {"position_id": 1, "ticker": "ACME", "horizon": "SHORT",
           "qty_open": 60, "avg_entry": 100.0, "r_unit": 4.0,
           "exit_policy": policy, "opened_ts": None, "last_price": None}
    pos.update(over)
    return pos


def bar(o=100.0, h=100.5, l=99.5, c=100.0):
    return {"ts": 1751900000, "open": o, "high": h, "low": l, "close": c}


# ---- L1 attribution ----------------------------------------------------------

def test_l1_initial_stop_attribution():
    a = evaluate_on_bar(make_pos(), bar(l=95.9, c=96.5), 0)
    assert len(a) == 1 and (a[0].kind, a[0].layer, a[0].qty) == ("EXIT", "STOP", 60)


def test_l1_breakeven_attribution():
    pos = make_pos(policy={"current_stop": 100.0, "stop_basis": "breakeven"})
    a = evaluate_on_bar(pos, bar(l=99.9, c=100.2), 0)
    assert (a[0].kind, a[0].layer) == ("EXIT", "BREAKEVEN")


def test_l1_trail_attribution():
    pos = make_pos(policy={"current_stop": 103.0, "stop_basis": "trail",
                           "hwm": 108.0})
    a = evaluate_on_bar(pos, bar(o=104, h=104, l=102.8, c=103.5), 0)
    assert (a[0].kind, a[0].layer) == ("EXIT", "TRAIL")


def test_l1_beats_l4_same_bar():
    """A bar that touches both the stop and the target: the stop wins —
    conservative attribution, full exit."""
    a = evaluate_on_bar(make_pos(), bar(h=110.0, l=95.0, c=100.0), 0)
    assert len(a) == 1 and a[0].layer == "STOP"


# ---- L5 invalidation -----------------------------------------------------------

class FakeFire:
    predicate_id = "close_below_prenews"
    detail = "persisted 2 bars"
    action = {"type": "exit"}


def test_l5_invalidation_full_exit():
    a = evaluate_on_bar(make_pos(), bar(), 0, [FakeFire()])
    assert (a[0].kind, a[0].layer, a[0].qty) == ("EXIT", "INVALIDATION", 60)


def test_l1_beats_l5_same_bar():
    a = evaluate_on_bar(make_pos(), bar(l=95.0), 0, [FakeFire()])
    assert a[0].layer == "STOP"


# ---- L3 time stop -----------------------------------------------------------------

def test_l3_time_stop_fires_when_stale():
    # age 2 sessions >= 2 window, progress 0.25R < 0.5R
    a = evaluate_on_bar(make_pos(), bar(c=101.0), 2)
    assert (a[0].kind, a[0].layer) == ("EXIT", "TIME")


def test_l3_holds_when_progressing():
    # progress 0.75R >= 0.5R min
    a = evaluate_on_bar(make_pos(), bar(h=103.2, c=103.0), 2)
    assert all(x.layer != "TIME" for x in a)


def test_l3_absent_for_long_profile():
    pos = make_pos(policy={"time_stop": None})
    a = evaluate_on_bar(pos, bar(c=100.5), 5)
    assert all(x.layer != "TIME" for x in a)


# ---- L4 realization ------------------------------------------------------------------

def test_l4_target_price():
    # 100 * (1 + 0.7*0.055) = 103.85
    assert realization_target(100.0, make_pos()["exit_policy"]) == 103.85


def test_l4_scale_out_half():
    a = evaluate_on_bar(make_pos(), bar(h=104.0, c=103.9), 0)
    scale = [x for x in a if x.kind == "SCALE_OUT"]
    assert scale and (scale[0].layer, scale[0].qty) == ("TARGET", 30)


def test_l4_only_once():
    pos = make_pos(policy={"scale_out_done": True})
    a = evaluate_on_bar(pos, bar(h=105.0, c=104.5), 0)
    assert not [x for x in a if x.kind == "SCALE_OUT"]


def test_l4_review_flag_for_long():
    pos = make_pos(policy={"realization": {"target_fraction": 0.7,
                                           "action": "review_flag"},
                           "time_stop": None})
    a = evaluate_on_bar(pos, bar(h=104.0, c=103.9), 0)
    ev = [x for x in a if x.kind == "EVENT" and x.event_type == "POSITION_REVIEW"]
    assert len(ev) == 1


# ---- L2 ratchets ---------------------------------------------------------------------

def test_l2_breakeven_moves_at_1R():
    a = evaluate_on_bar(make_pos(), bar(h=104.1, c=104.0), 0)   # +1.0R
    sets = [x for x in a if x.kind == "SET_STOP"]
    assert sets and (sets[0].new_stop, sets[0].new_basis) == (100.0, "breakeven")


def test_l2_no_breakeven_below_1R():
    a = evaluate_on_bar(make_pos(), bar(h=103.0, c=102.0), 0)   # +0.5R
    assert not [x for x in a if x.kind == "SET_STOP"]


def test_l2_trail_from_1_5R():
    # close 106 = +1.5R, hwm 106.5 -> trail = 106.5 - 2.5*2.0 = 101.5
    a = evaluate_on_bar(make_pos(), bar(h=106.5, c=106.0), 0)
    sets = [x for x in a if x.kind == "SET_STOP"]
    assert sets and (sets[0].new_stop, sets[0].new_basis) == (101.5, "trail")


def test_l2_tighten_only_never_loosens():
    # trail proposal 101.5 but current stop already 102 -> discarded
    pos = make_pos(policy={"current_stop": 102.0, "stop_basis": "trail",
                           "hwm": 106.5})
    a = evaluate_on_bar(pos, bar(h=106.5, c=106.0), 0)
    assert not [x for x in a if x.kind == "SET_STOP"]


def test_l2_trail_ratchets_with_new_high():
    pos = make_pos(policy={"current_stop": 101.5, "stop_basis": "trail",
                           "hwm": 106.5})
    a = evaluate_on_bar(pos, bar(h=109.0, l=105.0, c=108.5), 0)  # new hwm 109
    sets = [x for x in a if x.kind == "SET_STOP"]
    assert sets and sets[0].new_stop == 104.0            # 109 - 5.0


def test_hwm_tracked_without_ratchet():
    a = evaluate_on_bar(make_pos(), bar(h=102.0, c=101.0), 0)  # below 1R
    hwm_updates = [x for x in a if x.kind == "EVENT" and x.new_hwm == 102.0]
    assert len(hwm_updates) == 1


# ---- D1 overnight matrix ---------------------------------------------------------------

@pytest.mark.parametrize("unreal_r,age,frac,earn,expect,rule", [
    (2.0, 0, 0.9, True,  "EXIT", "earnings_next_session"),   # earnings trumps
    (0.5, 3, 0.2, False, "HOLD", "unrealized_R_threshold"),
    (0.3, 3, 0.2, None,  "HOLD", "unrealized_R_threshold"),  # boundary >=
    (0.1, 0, 0.3, None,  "HOLD", "young_position"),
    (0.1, 0, 0.6, None,  "EXIT", "stale_flat"),   # young but move captured
    (0.1, 1, 0.3, None,  "EXIT", "stale_flat"),   # not young anymore
    (-0.2, 2, -0.1, None, "EXIT", "stale_flat"),
])
def test_overnight_matrix(unreal_r, age, frac, earn, expect, rule):
    assert overnight_decision(unreal_r, age, frac, earn, ON_CFG) == (expect, rule)


def test_realized_move_fraction():
    assert realized_move_fraction(102.75, 100.0, 0.055) == pytest.approx(0.5)
    assert realized_move_fraction(100.0, 100.0, 0.0) == 0.0

