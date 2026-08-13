"""v0.12.28 unit tests — the four WDAY fixes (incident 2026-08-13).

Every test in this file is pinned to a real number from that session's
journal, so a future refactor that quietly restores the old behaviour fails
here with the incident in the failure message.

Reference facts (journal.decisions / c3-gate log, all ET):
  14:36:26  cluster 22084 opens, source alpaca_benzinga
  14:36:33  A1 ESCALATE conf 0.95 (M&A catalyst class)
  14:37:16  A2 THESIS up, magnitude_est 0.08, source_risk medium
  14:37:19  C3 DEFER -> mature_ts 14:40:00  (min_confirm_bars 3)
  14:37:56  cluster 22084 second item (similarity 0.810)
  14:40:07  RECHECK pct_move 0.07392 vol_mult 34.3   <- first legal look
  14:42:18  cluster 22084 third item (similarity 0.866)
  14:43:11  RECHECK pct_move 0.07392 vol_mult 34.3   <- byte-identical: stale
  14:46:16  RECHECK pct_move 0.18377 vol_mult 60.48
  15:05:47  VETO CREDIBILITY, minute 29, independent_outlets 1, required 2
"""
from datetime import datetime, timedelta, timezone

import pytest

from c3_gate.rules import (EHState, MarketState, evaluate, evaluate_eh,
                           growth_credit)
from c3_gate.service import (abandon_recheck, bars_mature_ts,
                             confirm_bars_for)
from c10_scanner.rules import looks_like_derivative
from c10_scanner.service import merge_universe

CFG = {
    "intraday_move_pct": 0.015, "intraday_vol_mult": 2.5,
    "intraday_window_min": 30, "extended_pct": 0.06,
    "open_blackout_min": 15, "handoff_gap_ratio": 0.5,
    "impact_medium_min": 0.02, "impact_high_min": 0.05,
    "required_outlets": {"low": {2: 1, 3: 1}, "medium": {2: 1, 3: 2},
                         "high": {2: 2, 3: 3}},
    "cluster_growth": {"enabled": True, "items_per_credit": 1,
                       "max_credit": 1},
    "min_confirm_bars": 3, "fast_confirm_bars": 1, "fast_urgency": ["high"],
    "abandon_when_extended": True, "max_bar_age_secs": 120,
    "confirm_recheck_secs": 180, "confirm_final_margin_secs": 45,
}

WDAY_THESIS = {"ticker": "WDAY", "direction": "up", "magnitude_est": 0.08,
               "source_risk": "medium"}


def state(**over):
    """Market state at the moment C3 first looked at WDAY, unless overridden."""
    base = dict(prenews_price=186.27, last_price=200.0, vol_mult=34.3,
                minutes_since_publish=4, news_in_session=True,
                minutes_since_open=310, gap_pct=None,
                corroboration_outlets=1, tier_min=2, cluster_items=1,
                bar_age_secs=20.0)
    base.update(over)
    return MarketState(**base)


# --------------------------------------------------------------------------
# 1. cluster-growth credit — the veto that actually fired
# --------------------------------------------------------------------------

def test_wday_single_item_still_vetoes_credibility():
    """One article, one outlet: unchanged. The credit must not turn a lone
    unconfirmed claim into a trade — that is the rule's whole purpose."""
    v = evaluate(WDAY_THESIS, state(cluster_items=1), CFG)
    assert v.verdict == "VETO"
    assert v.veto_reason == "CREDIBILITY"
    assert v.numbers["corroboration"]["effective_outlets"] == 1
    assert v.numbers["credibility"]["required_outlets"] == 2


def test_wday_second_article_satisfies_credibility():
    """14:37:56 — the wire's second, distinct write-up (0.810 similarity, so
    C2 admitted it as a NEW article rather than a dedup repost). One credit,
    effective outlets 2, requirement met."""
    v = evaluate(WDAY_THESIS, state(cluster_items=2, last_price=190.0), CFG)
    assert v.numbers["corroboration"]["growth_credit"] == 1
    assert v.numbers["corroboration"]["effective_outlets"] == 2
    assert v.verdict == "PASS", v.veto_reason


def test_growth_credit_is_capped():
    """Four articles do not out-vote two independent outlets."""
    assert growth_credit(1, 2, CFG) == 1
    assert growth_credit(1, 4, CFG) == 1
    assert growth_credit(1, 40, CFG) == 1


def test_tier3_single_source_high_impact_still_never_passes():
    """The baseline invariant, preserved by arithmetic: required 3, one real
    outlet plus the capped credit is 2. No amount of growth reaches it."""
    thesis = {**WDAY_THESIS, "source_risk": "medium"}
    v = evaluate(thesis, state(tier_min=3, cluster_items=99,
                               last_price=190.0), CFG)
    assert v.verdict == "VETO" and v.veto_reason == "CREDIBILITY"
    assert v.numbers["corroboration"]["effective_outlets"] == 2
    assert v.numbers["credibility"]["required_outlets"] == 3


def test_growth_credit_disabled_restores_old_behaviour():
    cfg = {**CFG, "cluster_growth": {"enabled": False}}
    v = evaluate(WDAY_THESIS, state(cluster_items=9, last_price=190.0), cfg)
    assert v.verdict == "VETO" and v.veto_reason == "CREDIBILITY"


def test_growth_credit_applies_to_eh_shadow_branch():
    s = EHState(prenews_price=186.27, last_price=190.0, bid=189.9, ask=190.1,
                spread_bps=10.0, minutes_since_publish=3, session="post",
                corroboration_outlets=1, tier_min=2, cluster_items=2)
    v = evaluate_eh(WDAY_THESIS, s, {**CFG, "eh_shadow": {"window_min": 30,
                                                          "max_spread_bps": 100}})
    assert v.verdict == "WOULD_TRADE", v.veto_reason


# --------------------------------------------------------------------------
# 2. fast-catalyst maturity — the deferral that ate the entry window
# --------------------------------------------------------------------------

def test_high_urgency_matures_two_minutes_earlier():
    """The literal WDAY clock: published 14:36, min_confirm_bars=3 put the
    first legal look at 14:40:00 — by which point the tape was +7.39% and
    past extended_pct forever. fast_confirm_bars=1 looks at 14:38:00."""
    published = datetime(2026, 8, 13, 18, 36, 26, tzinfo=timezone.utc)
    slow = bars_mature_ts(published, confirm_bars_for("medium", CFG))
    fast = bars_mature_ts(published, confirm_bars_for("high", CFG))
    assert slow == datetime(2026, 8, 13, 18, 40, tzinfo=timezone.utc)
    assert fast == datetime(2026, 8, 13, 18, 38, tzinfo=timezone.utc)


def test_fast_path_never_evaluates_on_zero_bars():
    """v0.11.10 stands: the floor is one COMPLETED bar, never zero."""
    assert confirm_bars_for("high", {**CFG, "fast_confirm_bars": 0}) == 1
    assert confirm_bars_for("high", {**CFG, "fast_confirm_bars": -5}) == 1


def test_fast_path_cannot_exceed_the_default():
    assert confirm_bars_for("high", {**CFG, "fast_confirm_bars": 9}) == 3


@pytest.mark.parametrize("urgency", ["medium", "low", None, "HIGH "])
def test_non_fast_urgencies_are_unchanged(urgency):
    assert confirm_bars_for(urgency, CFG) == 3


def test_fast_path_is_case_insensitive_and_disableable():
    assert confirm_bars_for("HIGH", CFG) == 1
    assert confirm_bars_for("high", {**CFG, "fast_urgency": []}) == 3


# --------------------------------------------------------------------------
# 3. abandon-early — 25 minutes and 9 re-checks in a provably dead state
# --------------------------------------------------------------------------

def test_abandon_once_past_extended():
    assert abandon_recheck(0.07392, CFG) is True      # the 14:40 look
    assert abandon_recheck(0.18377, CFG) is True      # the 14:46 look
    assert abandon_recheck(0.059, CFG) is False       # still reachable
    assert abandon_recheck(None, CFG) is False        # unknown != dead


def test_abandon_is_disableable():
    assert abandon_recheck(0.5, {**CFG, "abandon_when_extended": False}) is False


# --------------------------------------------------------------------------
# 4. stale bars may not manufacture an entry
# --------------------------------------------------------------------------

def test_stale_bar_blocks_a_pass():
    """14:40 and 14:43 returned byte-identical pct_move AND vol_mult three
    minutes apart on a stock printing 60x relative volume."""
    v = evaluate(WDAY_THESIS, state(cluster_items=2, last_price=190.0,
                                    bar_age_secs=185.0), CFG)
    assert v.verdict == "VETO" and v.veto_reason == "STALE_MARKETDATA"


def test_stale_check_is_skipped_when_age_unknown():
    v = evaluate(WDAY_THESIS, state(cluster_items=2, last_price=190.0,
                                    bar_age_secs=None), CFG)
    assert v.verdict == "PASS"


def test_stale_check_never_creates_a_pass():
    """A fresh bar does not rescue a signal that fails an earlier check."""
    v = evaluate(WDAY_THESIS, state(cluster_items=2, last_price=210.5,
                                    bar_age_secs=1.0), CFG)
    assert v.verdict == "VETO" and v.veto_reason == "GATE_EXTENDED"


# --------------------------------------------------------------------------
# 5. scanner universe — the lane that never saw the biggest mover on the tape
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sym", [
    "BBBY.WS", "PSQH.WS", "AHT.PRD", "AHT.PRG", "AHT.PRH", "AHT.PRI",
    "DAVEW", "EDBLW", "HOLOW", "KWMWW", "MRNOW", "ASTLW", "DFDVW", "BBLGW",
    "PECEW", "XYZ.U", "ABC.RT",
])
def test_derivative_shapes_rejected(sym):
    """Every symbol here is from the 2026-08-13 scan log."""
    assert looks_like_derivative(sym) is True


@pytest.mark.parametrize("sym", [
    "WDAY", "MU", "IREN", "ACHR", "BYND", "HTZ", "PATH", "OPEN", "CRWV",
    "SMCI", "HPE", "NBIS", "SNDK", "RIOT", "GOOGL", "TSLA",
])
def test_real_tickers_survive(sym):
    """A false positive here costs a real trade. Four-letter tickers ending
    in W/R/U are NOT matched — only the five-character Nasdaq convention."""
    assert looks_like_derivative(sym) is False


def test_merge_universe_unions_every_leg_without_duplicates():
    movers = [{"symbol": "JLHL"}, {"symbol": "APPS"}]
    by_volume = [{"symbol": "APPS"}, {"symbol": "SOXL"}]
    by_trades = [{"symbol": "WDAY"}, {"symbol": "SOXL"}]
    out = merge_universe(movers, by_volume, by_trades)
    assert [r["symbol"] for r in out] == ["JLHL", "APPS", "SOXL", "WDAY"]


def test_merge_universe_is_backward_compatible():
    """One actives leg behaves exactly as v0.12.17."""
    assert [r["symbol"] for r in
            merge_universe([{"symbol": "A"}], [{"symbol": "A"},
                                               {"symbol": "B"}])] == ["A", "B"]
