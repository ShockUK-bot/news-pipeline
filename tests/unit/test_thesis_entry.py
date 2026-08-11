"""v0.12.26 — C11 thesis-entry lane: the don't-chase gate, liquidity floor,
sizing, the C4-shape exit policy, the tighten-only dead-thesis exit, and
config pins.

Design context (the RIOT test): RIOT jumped +26% overnight on a
thesis-confirming catalyst the day after the store's first theses were
seeded. The news lane rightly refuses to chase such gaps — the only way to
be in that move is to already hold the beneficiary. This lane exists to do
that, and its don't-chase gate must SKIP a post-pop RIOT while admitting
quiet beneficiaries."""
from pathlib import Path

import pytest
import yaml

from c11_thesis.service import (extended_verdict, intent_id_for,
                                liquidity_verdict, materialize_thesis_policy,
                                size_position, tighten_stop)

CFG_PATH = Path(__file__).resolve().parents[2] / "config" / "thesis_entry.yaml"
PROFILES = Path(__file__).resolve().parents[2] / "config" / "exit_profiles.yaml"

CFG = {"extended": {"max_above_sma20_pct": 0.10,
                    "max_5session_gain_pct": 0.15},
       "liquidity": {"min_price": 3.0, "min_adv_dollars": 20_000_000}}


def bars(closes, volume=2_000_000):
    return [{"open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
             "volume": volume} for c in closes]


# --- don't-chase gate ------------------------------------------------------

def test_quiet_name_passes():
    skip, n = extended_verdict(bars([100.0] * 25), CFG)
    assert skip is False
    assert n["above_sma20_pct"] == 0.0


def test_riot_morning_is_skipped():
    """+26% overnight pop -> far above the 20-session mean AND over the
    5-session run-up cap. Both trips independently."""
    skip, n = extended_verdict(bars([100.0] * 24 + [126.0]), CFG)
    assert skip is True
    assert n["above_sma20_pct"] > 0.10
    assert n["gain_5session_pct"] > 0.15


def test_slow_grind_above_sma_skipped():
    closes = [100 + i * 0.8 for i in range(25)]        # steady climb
    skip, n = extended_verdict(bars(closes), CFG)
    assert skip is (n["above_sma20_pct"] > 0.10 or
                    n["gain_5session_pct"] > 0.15)


def test_insufficient_history_skips():
    skip, n = extended_verdict(bars([100.0] * 10), CFG)
    assert skip is True and "insufficient" in n["reason"]


# --- liquidity -------------------------------------------------------------

def test_liquidity_floor():
    skip, n = liquidity_verdict(bars([2.0] * 25), CFG)
    assert skip is True and n["reason"] == "price floor"
    skip, _ = liquidity_verdict(bars([100.0] * 25, volume=50_000), CFG)
    assert skip is True                                # $5M/day < $20M floor
    skip, _ = liquidity_verdict(bars([100.0] * 25, volume=2_000_000), CFG)
    assert skip is False


# --- sizing ----------------------------------------------------------------

def test_sizing_matches_risk_budget():
    # $100k * 0.25% = $250 budget; ATR $2, k=3 -> $6 stop -> 41 shares
    qty, n = size_position(100_000, 0.0025, 2.0, 3.0, min_viable_risk=200)
    assert qty == 41
    assert n["actual_risk"] == pytest.approx(41 * 6.0)


def test_sizing_skips_when_stop_exceeds_budget():
    # $250 budget, ATR $168 (a SNDK), k=3 -> $504 stop > budget -> qty 0
    qty, n = size_position(100_000, 0.0025, 168.0, 3.0, min_viable_risk=200)
    assert qty == 0 and n["reason"] == "stop wider than budget"


def test_sizing_enforces_min_viable_risk():
    # $250 budget, $60 stop -> 4 shares risking $240 — viable at 200,
    # rejected when the floor is 245
    qty, _ = size_position(100_000, 0.0025, 20.0, 3.0, min_viable_risk=200)
    assert qty == 4
    qty, n = size_position(100_000, 0.0025, 20.0, 3.0, min_viable_risk=245)
    assert qty == 0 and n["reason"] == "below min viable risk"


# --- exit policy shape (the keys C4 actually reads) ------------------------

def test_policy_has_every_key_c4_consumes():
    profile = yaml.safe_load(PROFILES.read_text())["profiles"]["thesis_v1"]
    p = materialize_thesis_policy(profile, limit_price=100.0, atr=2.0,
                                  invalidation=["counterparty denies"],
                                  expected_move=0.20)
    # _open_position re-materializes stops from these:
    assert p["initial_stop"]["k"] == 3.0
    assert p["initial_stop"]["price"] == 94.0
    assert p["catastrophe_stop_broker"]["price"] == 91.0
    assert p["atr_value"] == 2.0 and p["atr_14"] == 2.0
    # evaluate_on_bar / realization math:
    assert p["magnitude_est"] == 0.20
    assert p["realization"]["action"] == "review_flag"
    assert p["time_stop"] is None
    # engine overnight pass skips LONG horizon; profile must not force-flat
    assert p["overnight_hold"] == "default_hold"
    assert "force_flat_time_et" not in p
    # thesis death is a STORE event, never a price predicate:
    assert p["machine_invalidations"] == []
    assert p["news_invalidations"] == ["counterparty denies"]
    assert p["origin"] == "thesis" and p["profile"] == "thesis_v1"
    assert p["earnings_blackout_exit"] is False


# --- dead-thesis exit arm --------------------------------------------------

def test_tighten_stop_moves_up_only():
    policy = {"initial_stop": {"price": 90.0}}
    out = tighten_stop(policy, last_price=100.0)
    assert out is not None
    new_policy, new_stop = out
    assert new_stop == 99.5
    assert new_policy["current_stop"] == 99.5
    # never loosen: current stop already above the target -> no-op
    assert tighten_stop({"initial_stop": {"price": 90.0},
                         "current_stop": 99.9}, 100.0) is None


def test_intent_id_is_deterministic():
    a = intent_id_for("th-2026-001", "VST", "2026-08-12")
    assert a == "thesis-th-2026-001-VST-2026-08-12"
    assert a == intent_id_for("th-2026-001", "VST", "2026-08-12")


# --- config pins -----------------------------------------------------------

def test_config_is_coherent():
    cfg = yaml.safe_load(CFG_PATH.read_text())
    e = cfg["entry"]
    assert 0 < e["risk_pct"] <= 0.005, \
        "thesis lane must risk at most the news lane's 0.5%"
    assert e["max_new_per_day"] <= e["max_open_positions"]
    assert e["max_per_thesis"] <= e["max_open_positions"]
    assert 0 < e["chase_buffer_pct"] <= 0.05
    assert e["entry_blackout_min"] >= 15, "no entries in the first 15 min"
    assert cfg["management"]["exit_dead_theses"] is True


def test_thesis_profile_exists_and_holds_through_earnings():
    prof = yaml.safe_load(PROFILES.read_text())["profiles"]["thesis_v1"]
    assert prof["earnings_blackout_exit"] is False    # operator policy
    assert prof["overnight_hold"] == "default_hold"   # never force-flat
    assert prof["time_stop"] is None                  # store is the clock
    assert prof["initial_stop"]["k"] >= 2.5           # wide by design
