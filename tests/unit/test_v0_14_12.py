"""v0.14.12 — Gate Lab follow-ups (review 2026-09-15).

1. HANDOFF_UNMOVED: the open-handoff LONG needs a minimum move in its
   direction; down-theses are exempt; 0 disables.
2. fade_candidate: a final PRICED_IN / GATE_EXTENDED veto on a bullish
   thesis, 4-12% up, inside the first 90 minutes, is a fade candidate;
   everything else is not. Pure; the shadow branch in the service is not
   under unit test (DB), only its decision.
3. Config pins.
"""
from pathlib import Path

import yaml

from c3_gate.rules import (GateVerdict, MarketState, ShortContext, evaluate,
                           fade_candidate)

ROOT = Path(__file__).resolve().parents[2] / "config"

CFG = {"intraday_move_pct": 0.015, "intraday_vol_mult": 2.5,
       "intraday_window_min": 30, "extended_pct": 0.06,
       "open_blackout_min": 15, "handoff_gap_ratio": 0.5,
       "impact_medium_min": 0.02, "impact_high_min": 0.05,
       "required_outlets": {"low": {2: 1, 3: 1}, "medium": {2: 1, 3: 2},
                            "high": {2: 2, 3: 3}},
       "cluster_growth": {"enabled": False},
       "max_bar_age_secs": 120,
       "handoff_min_move_pct_long": 0.02,
       "fade": {"enabled": True, "min_move_pct": 0.04, "max_move_pct": 0.12,
                "window_max_min_after_open": 90}}


def thesis(direction="up", magnitude=0.03):
    return {"ticker": "ACME", "direction": direction, "horizon": "SHORT",
            "magnitude_est": magnitude, "source_risk": "low"}


def state(last, prenews=100.0, gap=0.0, mso=30, in_session=False, **over):
    s = MarketState(prenews_price=prenews, last_price=last, vol_mult=3.0,
                    minutes_since_publish=120, news_in_session=in_session,
                    minutes_since_open=mso, gap_pct=gap,
                    corroboration_outlets=2, tier_min=2,
                    pct_from_prior_close=(last / prenews - 1))
    for k, v in over.items():
        setattr(s, k, v)
    return s


SHORT_OK = ShortContext(enabled=True, etb_ok=True, pct_from_prior_close=-0.01)


# --- 1. HANDOFF_UNMOVED -----------------------------------------------------

def test_unmoved_bullish_handoff_is_vetoed():
    v = evaluate(thesis(), state(last=100.5), CFG)          # +0.5% only
    assert (v.verdict, v.rule, v.veto_reason) == ("VETO", "open_handoff",
                                                  "HANDOFF_UNMOVED")


def test_moved_bullish_handoff_still_passes():
    v = evaluate(thesis(), state(last=103.0), CFG)          # +3%, under 6%
    assert v.verdict == "PASS"


def test_bearish_handoff_is_exempt_from_the_floor():
    v = evaluate(thesis("down"), state(last=99.5), CFG, SHORT_OK)   # -0.5%
    assert v.verdict == "PASS"


def test_floor_zero_restores_old_behaviour():
    cfg = {**CFG, "handoff_min_move_pct_long": 0.0}
    assert evaluate(thesis(), state(last=100.5), cfg).verdict == "PASS"


def test_priced_in_and_extended_still_come_first():
    # gap >= 0.5 x magnitude -> PRICED_IN regardless of the floor
    v = evaluate(thesis(magnitude=0.03), state(last=102.0, gap=0.02), CFG)
    assert v.veto_reason == "PRICED_IN"
    v = evaluate(thesis(), state(last=108.0), CFG)
    assert v.veto_reason == "GATE_EXTENDED"


# --- 2. fade_candidate --------------------------------------------------------

def veto(reason, rule="open_handoff"):
    return GateVerdict("VETO", rule, reason, {})


def test_priced_in_bullish_in_band_is_a_fade_candidate():
    f = fade_candidate(thesis(), state(last=107.0, gap=0.05, mso=30),
                       veto("PRICED_IN"), CFG)
    assert f is not None and f["source_veto"] == "PRICED_IN"
    assert f["pct_move"] == 0.07 and f["band"] == [0.04, 0.12]


def test_extended_intraday_inside_window_qualifies():
    f = fade_candidate(thesis(), state(last=109.0, mso=45, in_session=True),
                       veto("GATE_EXTENDED", "intraday"), CFG)
    assert f is not None and f["source_rule"] == "intraday"


def test_below_band_is_not_a_fade():
    assert fade_candidate(thesis(), state(last=103.0, mso=30),
                          veto("PRICED_IN"), CFG) is None


def test_capitulation_band_is_not_a_fade():
    assert fade_candidate(thesis(), state(last=113.0, mso=30),
                          veto("GATE_EXTENDED"), CFG) is None


def test_outside_the_open_window_is_not_a_fade():
    assert fade_candidate(thesis(), state(last=107.0, mso=120),
                          veto("PRICED_IN"), CFG) is None
    assert fade_candidate(thesis(), state(last=107.0, mso=5),
                          veto("PRICED_IN"), CFG) is None
    assert fade_candidate(thesis(), state(last=107.0, mso=None),
                          veto("PRICED_IN"), CFG) is None


def test_other_vetoes_and_passes_are_not_fades():
    assert fade_candidate(thesis(), state(last=107.0), veto("CREDIBILITY"), CFG) is None
    assert fade_candidate(thesis(), state(last=107.0),
                          GateVerdict("PASS", "open_handoff", None, {}), CFG) is None


def test_bearish_theses_are_never_fades():
    assert fade_candidate(thesis("down"), state(last=93.0),
                          veto("GATE_EXTENDED"), CFG) is None


def test_fade_disabled_by_config():
    cfg = {**CFG, "fade": {"enabled": False}}
    assert fade_candidate(thesis(), state(last=107.0), veto("PRICED_IN"), cfg) is None


# --- 3. config pins -----------------------------------------------------------

def test_gate_yaml_pins():
    g = yaml.safe_load((ROOT / "gate.yaml").read_text())["gate"]
    assert g["handoff_min_move_pct_long"] > 0
    f = g["fade"]
    assert f["enabled"] is True
    assert 0 < f["min_move_pct"] < f["max_move_pct"] <= 0.15
    assert f["window_max_min_after_open"] >= g["open_blackout_min"]


def test_fade_lane_flag_exists_but_cannot_order():
    s = yaml.safe_load((ROOT / "shorting.yaml").read_text())["shorting"]
    assert s["lanes"]["fade"] is True
    # nothing in A3 selects a profile for origin 'fade': the shadow branch
    # lives in C3 and never enqueues — pinned by grep, not by behaviour
    src = (Path(__file__).resolve().parents[2] / "src" / "c3_gate" /
           "service.py").read_text()
    assert "enqueue(" not in src.split("async def _fade_shadow")[1].split(
        "async def short_ctx")[0]
