"""v0.14.11 — A6 review verdicts reach the exit ladder (no human in the loop).

1. review_exit_due: n consecutive "exit / thesis_intact=false" nightly
   verdicts arm the exit; anything less, mixed, or "exit but intact" does not.
2. management_action: dead thesis still wins; ACTIVE + bridge -> REVIEW;
   ACTIVE without the streak -> None; bridge disabled by 0.
3. Config pins: the knob exists, and A5's staleness clock equals A6's.
"""
from pathlib import Path

import yaml

from c11_thesis.service import management_action, review_exit_due

ROOT = Path(__file__).resolve().parents[2] / "config"


def v(verdict="exit", intact=False, stale="stale"):
    return {"verdict": verdict, "thesis_intact": intact, "staleness": stale,
            "confidence": 0.55, "rationale": "x"}


# --- review_exit_due --------------------------------------------------------

def test_three_broken_exits_arm():
    assert review_exit_due([v(), v(), v()], 3) is True


def test_riot_shape_arms_with_more_than_n():
    # 13 nightly exits in a row (RIOT 08-28..09-14): newest n suffice
    assert review_exit_due([v()] * 13, 3) is True


def test_two_of_three_do_not_arm():
    assert review_exit_due([v(), v()], 3) is False


def test_a_hold_in_the_window_resets():
    assert review_exit_due([v(), v("hold", True, "fresh"), v()], 3) is False


def test_exit_with_thesis_intact_does_not_arm():
    # "exit" for a non-thesis reason (e.g. trim/overextension) is not a
    # broken thesis; the bridge needs thesis_intact=false on every night
    assert review_exit_due([v(intact=True), v(), v()], 3) is False


def test_older_holds_do_not_matter():
    assert review_exit_due([v(), v(), v(), v("hold", True)], 3) is True


def test_zero_disables():
    assert review_exit_due([v(), v(), v()], 0) is False


def test_missing_fields_are_not_exit():
    assert review_exit_due([{}, v(), v()], 3) is False


# --- management_action ------------------------------------------------------

MCFG = {"exit_dead_theses": True, "exit_on_review_verdicts": 3}


def test_dead_thesis_still_wins():
    assert management_action("EXPIRED", [], MCFG) == "DEAD"
    assert management_action("INVALIDATED", [v()] * 3, MCFG) == "DEAD"


def test_active_with_streak_is_review():
    assert management_action("ACTIVE", [v()] * 3, MCFG) == "REVIEW"


def test_active_without_streak_is_none():
    assert management_action("ACTIVE", [v(), v("hold", True)], MCFG) is None


def test_bridge_disabled_by_zero():
    assert management_action("ACTIVE", [v()] * 5,
                             {"exit_on_review_verdicts": 0}) is None


def test_dead_exit_switch_off_leaves_dead_thesis_alone():
    assert management_action("EXPIRED", [], {"exit_dead_theses": False}) is None


# --- config pins ------------------------------------------------------------

def test_bridge_knob_is_on():
    cfg = yaml.safe_load((ROOT / "thesis_entry.yaml").read_text())
    n = int(cfg["management"]["exit_on_review_verdicts"])
    assert 1 <= n <= 5, "bridge should be on and need more than one night"


def test_staleness_clocks_agree():
    a5 = yaml.safe_load((ROOT / "a5.yaml").read_text())
    a6 = yaml.safe_load((ROOT / "a6.yaml").read_text())
    assert a5["store"]["stale_weeks"] == a6["review"]["stale_weeks"], (
        "A5 thesis expiry and A6 review staleness must use the same clock")
