"""v0.14.5 unit tests — the analyst thinking-leak fix (incident 2026-09-03).

That day the analyst model (qwen3.6-27b, a hybrid reasoning model) ran with
thinking ON — `disable_thinking` was never added to a2.yaml when the slot
moved off the old non-thinking qwen3-32b. It narrated prose inside its
<think> block; the response_format grammar constrains `content` only, so
128/196 analyst calls (~65%) returned "output is not valid JSON: Expecting
value: line 1 column 1" and the system traded nothing all day. Every raw
output began "Let me analyze this signal..." instead of "{".

Tests are pinned to that day's numbers and to the config precedent
(A4/A5/A6/A7/A8 have set disable_thinking since v0.12.4).
"""
import pathlib

import pytest
import yaml

from a2_analyst.service import analyst_health

REPO = pathlib.Path(__file__).resolve().parents[2]


def _cfg(name):
    return yaml.safe_load((REPO / "config" / name).read_text())


# ---- the fix: every qwen3.6-27b JSON producer disables thinking -------------

@pytest.mark.parametrize("cfg_name", ["a2.yaml", "a12.yaml", "risk.yaml"])
def test_thinking_disabled_on_the_reasoning_model(cfg_name):
    """The 2026-09-03 break: a thinking model on the live trade path without
    disable_thinking. All three live-path 27b consumers must set it true."""
    m = _cfg(cfg_name)["model"]
    assert m["model_id"].startswith("qwen3.6-27b"), "test guards the 27b slot"
    assert m.get("disable_thinking") is True, (
        f"{cfg_name} runs a thinking model without disable_thinking — "
        "this is the 2026-09-03 65%-invalid-output bug")


def test_the_heavy_slots_still_disable_thinking_too():
    """Regression guard for the original v0.12.4 fix — don't undo it."""
    for name in ("a4.yaml", "a5.yaml", "a7.yaml", "a8.yaml"):
        text = (REPO / "config" / name).read_text()
        assert "disable_thinking: true" in text, name


# ---- the guard: a silent invalid-output storm becomes DEGRADED --------------

def test_health_ok_below_min_sample():
    # Two bad calls is not yet evidence of a storm.
    status, _ = analyst_health([False, False], min_sample=10)
    assert status == "OK"


def test_health_ok_when_mostly_valid():
    recent = [True] * 18 + [False] * 2       # 10% invalid
    status, detail = analyst_health(recent, min_sample=10, max_invalid_frac=0.5)
    assert status == "OK"
    assert "invalid 2/20" in detail


def test_health_degraded_on_a_storm():
    # The 2026-09-03 shape: ~65% invalid over a full window.
    recent = [False] * 13 + [True] * 7       # 65% invalid
    status, detail = analyst_health(recent, min_sample=10, max_invalid_frac=0.5)
    assert status == "DEGRADED"
    assert "65%" in detail
    assert "disable_thinking" in detail       # points the operator at the fix


def test_health_boundary_is_inclusive():
    recent = [False] * 10 + [True] * 10       # exactly 50%
    status, _ = analyst_health(recent, min_sample=10, max_invalid_frac=0.5)
    assert status == "DEGRADED"


def test_health_all_good_is_ok():
    status, detail = analyst_health([True] * 20, min_sample=10)
    assert status == "OK" and "invalid 0/20" in detail


# ---- large-cap volume handling (the MSTR 2.94x miss) ------------------------

from c10_scanner.rules import (CandidateMetrics, filter_candidate,
                               liquidity_term, rel_volume_bar, score_candidate)

SCFG = _cfg("scanner.yaml")["scanner"]


def _mstr(**over):
    """MSTR at 09:51 on 2026-09-03, from journal.scanner_candidates:
    move +7.44%, rel_vol 2.94x, spread 3.78bps, HOD 8 min ago. ADV ~$5B."""
    m = CandidateMetrics(
        ticker="MSTR", price=130.33, prev_close=127.99, move_pct=0.07436,
        adv20_dollars=5_000_000_000.0, rel_volume=2.94, minutes_since_hod=8,
        spread_bps=3.78, luld_headroom_pct=0.09, vwap=129.0, day_high=131.0,
        detected_ts="2026-09-03T13:51:16+00:00")
    for k, v in over.items():
        setattr(m, k, v)
    return m


def test_mstr_294x_now_passes_the_relvol_filter():
    """The whole miss: 2.94x < the flat 3.0x bar. With the large-cap tier a
    $5B-ADV name needs only 2.0x, so MSTR clears it."""
    assert rel_volume_bar(_mstr(), SCFG) == 2.0
    assert filter_candidate(_mstr(), SCFG) is None


def test_a_microcap_still_needs_the_full_3x():
    """A thin name ($30M ADV) at 2.94x is still REL_VOLUME-rejected — the
    relaxed bar is for deep liquidity only."""
    micro = _mstr(adv20_dollars=30_000_000.0, price=8.0)
    assert rel_volume_bar(micro, SCFG) == 3.0
    assert filter_candidate(micro, SCFG) == "REL_VOLUME"


def test_mstr_now_scores_over_the_emit_floor():
    """Filter fix alone wasn't enough — at 2.94x the OLD score was ~0.50,
    under the 0.60 floor. The liquidity term lifts a deeply-liquid mover over
    it so MSTR emits EARLY (+7.4%), with room to run to +13.5%."""
    s = score_candidate(_mstr(), SCFG)
    assert s >= float(SCFG["min_emit_score"]), s


def test_liquidity_term_rewards_size_not_thinness():
    big = liquidity_term(5_000_000_000.0, SCFG)
    small = liquidity_term(30_000_000.0, SCFG)
    assert big == 1.0                       # >= $2.5B cap
    assert small < 0.1                      # just above the $25M floor
    assert liquidity_term(None, SCFG) == 0.0


def test_liquidity_alone_cannot_carry_a_weak_mover():
    """A huge but barely-moving name must NOT emit on size alone."""
    dull = _mstr(move_pct=0.041, rel_volume=2.0, minutes_since_hod=55)
    assert score_candidate(dull, SCFG) < float(SCFG["min_emit_score"])


def test_score_still_orders_by_conviction():
    assert score_candidate(_mstr(rel_volume=8.0), SCFG) \
        > score_candidate(_mstr(rel_volume=3.0), SCFG)
    assert score_candidate(_mstr(move_pct=0.13), SCFG) \
        > score_candidate(_mstr(move_pct=0.045), SCFG)


def test_config_can_restore_pre_v0_14_5_behaviour():
    off = {**SCFG, "large_cap_adv_dollars": 0,
           "score_w_rel": 0.45, "score_w_liquidity": 0.0}
    assert rel_volume_bar(_mstr(), off) == 3.0
    assert filter_candidate(_mstr(), off) == "REL_VOLUME"
