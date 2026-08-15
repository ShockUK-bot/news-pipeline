"""v0.13.0 short selling — the sign-flip proof matrix.

Four layers under test, no DB, no network:
  1. common.direction — the single source of directional arithmetic.
  2. C3 direction gate + signed confirmation (news / EH shadow / scanner).
  3. A3 sizing — short stops above entry, short-book clips, heat.
  4. C4 exit engine — every layer mirrored, parametrized LONG vs SHORT
     over the same geometry so a sign bug cannot hide.
  5. FakeBroker — sell-from-flat opens a short; buy covers; flip-through-zero.
"""
from __future__ import annotations

import pytest

from common import direction as D
from common.broker import BrokerOrder, FakeBroker
from c3_gate.rules import (EHState, MarketState, ScannerState, ShortContext,
                           direction_gate, evaluate, evaluate_eh,
                           evaluate_scanner)
from a3_risk.sizing import SizingInputs, open_risk_dollars, size_entry
from c4_exec.exits import evaluate_on_bar, realization_target
from c4_exec.overnight import realized_move_fraction

# ---------------------------------------------------------------------------
# 1. common.direction invariants
# ---------------------------------------------------------------------------

def test_r_unit_positive_both_sides():
    assert D.r_unit("LONG", 100.0, 96.0) == 4.0
    assert D.r_unit("SHORT", 100.0, 104.0) == 4.0     # stop ABOVE entry


def test_entry_stop_sides():
    assert D.entry_stop("LONG", 100.0, 4.0) == 96.0
    assert D.entry_stop("SHORT", 100.0, 4.0) == 104.0


def test_pnl_and_progress_signs():
    # both sides winning -> positive
    assert D.pnl("LONG", 100.0, 105.0, 10) == 50.0
    assert D.pnl("SHORT", 100.0, 95.0, 10) == 50.0
    assert D.r_progress("LONG", 100.0, 4.0, 104.0) == 1.0
    assert D.r_progress("SHORT", 100.0, 4.0, 96.0) == 1.0
    # both sides losing -> negative
    assert D.pnl("SHORT", 100.0, 105.0, 10) == -50.0
    assert D.r_progress("SHORT", 100.0, 4.0, 104.0) == -1.0


def test_tighter_is_side_aware():
    assert D.is_tighter("LONG", 98.0, 96.0)           # higher = tighter
    assert not D.is_tighter("LONG", 95.0, 96.0)
    assert D.is_tighter("SHORT", 102.0, 104.0)        # LOWER = tighter
    assert not D.is_tighter("SHORT", 105.0, 104.0)


def test_targets_and_stops_hit_on_the_right_side():
    assert D.realization_target("LONG", 100.0, 0.7, 0.10) == 107.0
    assert D.realization_target("SHORT", 100.0, 0.7, 0.10) == 93.0
    bar = {"open": 100, "high": 108, "low": 99, "close": 101}
    assert D.target_hit("LONG", bar, 107.0)
    assert not D.target_hit("SHORT", bar, 93.0)
    bar2 = {"open": 100, "high": 101, "low": 92.5, "close": 93}
    assert D.target_hit("SHORT", bar2, 93.0)
    assert D.stop_hit("SHORT", {"high": 104.2, "low": 100}, 104.0)
    assert not D.stop_hit("SHORT", {"high": 103.0, "low": 100}, 104.0)


def test_watermark_and_trail():
    # short watermark is the LOW-water mark; trail sits ABOVE it
    bar = {"high": 105.0, "low": 94.0}
    assert D.watermark("LONG", 103.0, bar) == 105.0
    assert D.watermark("SHORT", 95.0, bar) == 94.0
    assert D.trail_from("LONG", 105.0, 3.0) == 102.0
    assert D.trail_from("SHORT", 94.0, 3.0) == 97.0


def test_marketable_exit_crosses_toward_fill():
    assert D.marketable_exit("LONG", 100.0, 0.003) == 99.7
    assert D.marketable_exit("SHORT", 100.0, 0.003) == 100.3


def test_open_risk_nonzero_for_shorts():
    # the long-only formula returned 0 for every short — heat caps depended
    # on this being fixed
    assert D.open_risk("SHORT", 100.0, 104.0, 10) == 40.0
    assert D.open_risk("SHORT", 100.0, 99.0, 10) == 0.0   # stop through entry
    assert D.open_risk("LONG", 100.0, 96.0, 10) == 40.0


def test_intent_mappings():
    assert D.ENTRY_INTENT == {"LONG": "BUY", "SHORT": "SELL_SHORT"}
    assert D.EXIT_INTENT == {"LONG": "SELL", "SHORT": "BUY_TO_COVER"}
    assert D.BROKER_SIDE["SELL_SHORT"] == "SELL"
    assert D.BROKER_SIDE["BUY_TO_COVER"] == "BUY"
    assert D.side_for("up") == "LONG" and D.side_for("down") == "SHORT"


# ---------------------------------------------------------------------------
# 2. C3 direction gate + signed confirmation
# ---------------------------------------------------------------------------

GATE_CFG = {
    "intraday_move_pct": 0.015, "intraday_vol_mult": 2.5,
    "intraday_window_min": 30, "extended_pct": 0.06,
    "open_blackout_min": 15, "handoff_gap_ratio": 0.5,
    "impact_medium_min": 0.02, "impact_high_min": 0.05,
    "required_outlets": {"low": {2: 1, 3: 1}, "medium": {2: 1, 3: 2},
                         "high": {2: 2, 3: 3}},
}

SHORT_OK = ShortContext(enabled=True, etb_ok=True, ssr_veto=True,
                        pct_from_prior_close=-0.03)


def state(**over):
    base = dict(prenews_price=100.0, last_price=98.0, vol_mult=3.0,
                minutes_since_publish=10, news_in_session=True,
                minutes_since_open=120, gap_pct=None,
                corroboration_outlets=2, tier_min=2,
                pct_from_prior_close=-0.03)
    base.update(over)
    return MarketState(**base)


def thesis_d(**over):
    base = {"ticker": "ACME", "direction": "down", "magnitude_est": 0.055,
            "source_risk": "low"}
    base.update(over)
    return base


def test_direction_gate_matrix():
    assert direction_gate("up", None) is None
    assert direction_gate("down", None) == "LONG_ONLY"
    assert direction_gate("down",
                          ShortContext(enabled=False, etb_ok=False)) == "LONG_ONLY"
    assert direction_gate("down",
                          ShortContext(enabled=True, etb_ok=False)) == "SHORT_UNAVAILABLE"
    assert direction_gate("down", ShortContext(
        enabled=True, etb_ok=True, pct_from_prior_close=-0.12)) == "SSR_RESTRICTED"
    # unknown prior-close move fails CLOSED while ssr_veto is on
    assert direction_gate("down", ShortContext(
        enabled=True, etb_ok=True, pct_from_prior_close=None)) == "SSR_RESTRICTED"
    assert direction_gate("down", ShortContext(
        enabled=True, etb_ok=True, ssr_veto=False,
        pct_from_prior_close=None)) is None
    assert direction_gate("down", SHORT_OK) is None


def test_short_intraday_pass_on_confirming_down_move():
    v = evaluate(thesis_d(), state(), GATE_CFG, SHORT_OK)
    assert (v.verdict, v.rule, v.veto_reason) == ("PASS", "intraday", None)
    assert v.numbers["signed_move"] == 0.02               # fell 2% = confirms


def test_short_no_confirm_when_stock_rises():
    v = evaluate(thesis_d(), state(last_price=101.0), GATE_CFG, SHORT_OK)
    assert v.veto_reason == "GATE_NO_CONFIRM"             # moved AGAINST thesis


def test_short_gate_extended_after_big_fall():
    v = evaluate(thesis_d(), state(last_price=93.0), GATE_CFG, SHORT_OK)
    assert v.veto_reason == "GATE_EXTENDED"               # -7% already gone


def test_long_path_byte_identical_with_shorting_on():
    up = {"ticker": "ACME", "direction": "up", "magnitude_est": 0.055,
          "source_risk": "low"}
    a = evaluate(up, state(last_price=102.0), GATE_CFG)
    b = evaluate(up, state(last_price=102.0), GATE_CFG, SHORT_OK)
    assert (a.verdict, a.veto_reason) == (b.verdict, b.veto_reason) == ("PASS", None)


def test_short_handoff_priced_in_on_gap_down():
    v = evaluate(thesis_d(), state(news_in_session=False, gap_pct=-0.04,
                                   last_price=99.0),
                 GATE_CFG, SHORT_OK)
    assert v.veto_reason == "PRICED_IN"                   # gapped 73% of est


def test_eh_shadow_short_sells_the_bid():
    s = EHState(prenews_price=100.0, last_price=98.0, bid=97.9, ask=98.1,
                spread_bps=20.0, minutes_since_publish=5, session="pre",
                corroboration_outlets=2, tier_min=2)
    cfg = {**GATE_CFG, "eh_shadow": {"window_min": 30, "max_spread_bps": 100}}
    v = evaluate_eh(thesis_d(), s, cfg, SHORT_OK)
    assert v.verdict == "WOULD_TRADE"
    assert v.numbers["hypothetical_entry"] == 97.9        # the bid, not the ask


SCFG = {"stale_max_min": 20, "stale_run_pct": 0.02, "require_above_vwap": True,
        "range30_min_pos": 0.6, "parabolic_bar_ratio": 3.5,
        "max_spread_bps": 30}


def test_scanner_short_mirrors_structure():
    # a loser continuing lower: below VWAP, at the LOW of the recent range
    s = ScannerState(last_price=95.0, detect_price=95.5,
                     minutes_since_detect=5.0, vwap=96.5, range30_pos=0.1,
                     bar5_range_ratio=1.2, spread_bps=12.0)
    v = evaluate_scanner(thesis_d(), s, SCFG, SHORT_OK)
    assert (v.verdict, v.veto_reason) == ("PASS", None)
    # bouncing back above VWAP = mean reversion against the short
    s2 = ScannerState(last_price=97.0, detect_price=95.5,
                      minutes_since_detect=5.0, vwap=96.5, range30_pos=0.1,
                      bar5_range_ratio=1.2, spread_bps=12.0)
    assert evaluate_scanner(thesis_d(), s2, SCFG,
                            SHORT_OK).veto_reason == "SCANNER_STALE" \
        or evaluate_scanner(thesis_d(), s2, SCFG,
                            SHORT_OK).veto_reason == "SCANNER_STRUCTURE"


# ---------------------------------------------------------------------------
# 3. A3 sizing
# ---------------------------------------------------------------------------

CAPITAL = {"risk_per_trade_pct": 0.005, "max_position_notional_pct": 0.15,
           "max_portfolio_heat_pct": 0.03,
           "heat_split": {"SHORT": 0.02, "LONG": 0.01},
           "max_sector_heat_pct": 0.015, "min_viable_risk_fraction": 0.5}
LIMITS = {"max_trades_per_day_default": 5, "adv_participation_max": 0.01,
          "spread_max_bps": 40, "entry_blackout_final_min": 15}
PROFILE = {"initial_stop": {"method": "atr", "k": 2.0},
           "catastrophe": {"method": "atr", "k": 3.5},
           "breakeven_at_R": 1.0,
           "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
           "time_stop": {"window": "thesis", "min_progress_R": 0.5},
           "realization": {"target_fraction": 0.7, "action": "scale_out_50"},
           "earnings_blackout_exit": True, "overnight_hold": "eod_rule_v1"}
SHORT_CFG = {"max_short_heat_pct": 0.015, "max_gross_short_notional_pct": 0.30,
             "dividend_blackout_sessions": 2}


def inputs(**over):
    base = dict(effective_capital=100_000.0, settled_cash=100_000.0,
                ref_price=100.0, bid=99.95, ask=100.05, spread_bps=10.0,
                atr_14=2.0, adv_20d=5_000_000,
                open_heat={"SHORT": 0.0, "LONG": 0.0},
                deployed_notional=0.0, trades_today=0,
                minutes_to_close=180, earnings_next_sessions=5)
    base.update(over)
    return SizingInputs(**base)


def test_short_sizing_geometry():
    r = size_entry(inputs(side="SHORT", regt_buying_power=200_000.0),
                   CAPITAL, LIMITS, PROFILE, "SHORT", 2.0,
                   shorting_cfg=SHORT_CFG)
    assert r.verdict == "SIZE"
    assert r.limit_price < 99.95 or r.limit_price == pytest.approx(99.85, abs=0.11)
    assert r.initial_stop > r.limit_price                 # stop ABOVE entry
    assert r.catastrophe_stop > r.initial_stop            # catastrophe further
    assert r.initial_stop == pytest.approx(r.limit_price + 4.0, abs=0.01)
    assert r.qty > 0


def test_short_buying_power_clip():
    r = size_entry(inputs(side="SHORT", regt_buying_power=0.0),
                   CAPITAL, LIMITS, PROFILE, "SHORT", 2.0,
                   shorting_cfg=SHORT_CFG)
    assert r.verdict == "VETO" and r.veto_reason == "SIZE_CLIPPED"
    assert r.numbers["clips"]["buying_power"] == 0.0


def test_short_heat_cap_binds():
    r = size_entry(inputs(side="SHORT", regt_buying_power=200_000.0,
                          open_short_heat=1_500.0),      # cap = 1.5% = $1500
                   CAPITAL, LIMITS, PROFILE, "SHORT", 2.0,
                   shorting_cfg=SHORT_CFG)
    assert r.verdict == "VETO" and r.veto_reason == "SIZE_CLIPPED"
    assert r.numbers["clips"]["short_heat"] == 0.0


def test_short_carries_dividend_unknown_flag():
    r = size_entry(inputs(side="SHORT", regt_buying_power=200_000.0),
                   CAPITAL, LIMITS, PROFILE, "SHORT", 2.0,
                   shorting_cfg=SHORT_CFG)
    assert "DIVIDEND_UNKNOWN" in r.flags


def test_short_ex_dividend_veto_when_known():
    r = size_entry(inputs(side="SHORT", regt_buying_power=200_000.0,
                          ex_dividend_next_sessions=1),
                   CAPITAL, LIMITS, PROFILE, "SHORT", 2.0,
                   shorting_cfg=SHORT_CFG)
    assert r.verdict == "VETO" and r.veto_reason == "EX_DIVIDEND"


def test_long_sizing_unchanged_by_shorting_cfg():
    a = size_entry(inputs(), CAPITAL, LIMITS, PROFILE, "SHORT", 2.0)
    b = size_entry(inputs(), CAPITAL, LIMITS, PROFILE, "SHORT", 2.0,
                   shorting_cfg=SHORT_CFG)
    assert (a.verdict, a.qty, a.limit_price, a.initial_stop) \
        == (b.verdict, b.qty, b.limit_price, b.initial_stop)
    assert a.initial_stop < a.limit_price


def test_open_risk_dollars_side_aware():
    assert open_risk_dollars(10, 100.0, 104.0, "SHORT") == 40.0
    assert open_risk_dollars(10, 100.0, 96.0, "LONG") == 40.0
    assert open_risk_dollars(10, 100.0, 96.0, "SHORT") == 0.0


# ---------------------------------------------------------------------------
# 4. C4 exit engine, parametrized over side (same geometry, mirrored prices)
# ---------------------------------------------------------------------------

def make_pos(side: str, **policy_over):
    """Entry 100, stop 4 away on the losing side, r_unit 4, ATR 2."""
    sgn = 1 if side == "LONG" else -1
    policy = {"profile": "short_term_v1", "side": side,
              "initial_stop": {"method": "atr", "k": 2.0,
                               "price": 100.0 - sgn * 4.0},
              "catastrophe_stop_broker": {"k": 3.5, "price": 100.0 - sgn * 7.0},
              "breakeven_at_R": 1.0,
              "trail": {"activate_at_R": 1.5, "method": "atr", "k": 2.5},
              "time_stop": {"window": "2_sessions", "min_progress_R": 0.5},
              "realization": {"target_fraction": 0.7, "action": "scale_out_50"},
              "magnitude_est": 0.10, "atr_14": 2.0, "atr_value": 2.0}
    policy.update(policy_over)
    return {"position_id": 1, "ticker": "ACME", "qty_open": 10,
            "avg_entry": 100.0, "r_unit": 4.0, "side": side,
            "exit_policy": policy}


def mirror_bar(side: str, win: float, lose: float, close: float) -> dict:
    """A bar expressed in 'favorable excursion' terms: win/lose/close are
    signed moves from entry in the position's favor; mirrored to prices."""
    sgn = 1 if side == "LONG" else -1
    prices = [100.0 + sgn * win, 100.0 - sgn * lose, 100.0 + sgn * close]
    return {"ts": None, "open": 100.0, "high": max(prices + [100.0]),
            "low": min(prices + [100.0]), "close": 100.0 + sgn * close}


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_l1_stop_fires_on_losing_side(side):
    bar = mirror_bar(side, win=0.5, lose=4.5, close=-4.2)
    actions = evaluate_on_bar(make_pos(side), bar, 0)
    assert actions[0].kind == "EXIT" and actions[0].layer == "STOP"


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_l4_realization_scales_out_at_mirrored_target(side):
    # target = entry * (1 +/- 0.7*0.10) = 107 / 93
    pos = make_pos(side)
    assert realization_target(100.0, pos["exit_policy"]) == \
        (107.0 if side == "LONG" else 93.0)
    bar = mirror_bar(side, win=7.2, lose=0.5, close=6.8)
    actions = evaluate_on_bar(pos, bar, 0)
    assert any(a.kind == "SCALE_OUT" and a.layer == "TARGET" and a.qty == 5
               for a in actions)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_l2_breakeven_then_trail_ratchet(side):
    sgn = 1 if side == "LONG" else -1
    pos = make_pos(side)
    # +1.0R = 4 in favor -> breakeven
    bar = mirror_bar(side, win=4.2, lose=0.2, close=4.0)
    actions = evaluate_on_bar(pos, bar, 0)
    ratchets = [a for a in actions if a.kind == "SET_STOP"]
    assert ratchets and ratchets[0].new_basis == "breakeven"
    assert ratchets[0].new_stop == 100.0
    # +2R with watermark 8 in favor -> trail at watermark -/+ 2.5*2.
    # Bar built explicitly: entirely in-profit territory (never touches the
    # breakeven stop at 100), so only the ratchet layer can act.
    pos2 = make_pos(side, current_stop=100.0, stop_basis="breakeven",
                    hwm=100.0 + sgn * 8.0)
    prices2 = [100.0 + sgn * 8.0, 100.0 + sgn * 6.0, 100.0 + sgn * 7.0]
    bar2 = {"ts": None, "open": prices2[1], "high": max(prices2),
            "low": min(prices2), "close": prices2[2]}
    actions2 = evaluate_on_bar(pos2, bar2, 0)
    ratchets2 = [a for a in actions2 if a.kind == "SET_STOP"]
    assert ratchets2 and ratchets2[0].new_basis == "trail"
    assert ratchets2[0].new_stop == pytest.approx(100.0 + sgn * 3.0, abs=0.01)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_l2_tighten_only_never_loosens(side):
    sgn = 1 if side == "LONG" else -1
    # stop already tighter than any proposal -> no SET_STOP
    pos = make_pos(side, current_stop=100.0 + sgn * 5.0, stop_basis="trail",
                   hwm=100.0 + sgn * 8.0)
    bar = mirror_bar(side, win=6.0, lose=-5.5, close=5.8)
    actions = evaluate_on_bar(pos, bar, 0)
    assert not [a for a in actions if a.kind == "SET_STOP"]


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_l3_time_stop_uses_signed_progress(side):
    pos = make_pos(side)
    # 2 sessions old, only 0.25R in favor -> TIME exit
    bar = mirror_bar(side, win=1.2, lose=0.5, close=1.0)
    actions = evaluate_on_bar(pos, bar, 2)
    assert actions[0].kind == "EXIT" and actions[0].layer == "TIME"


def test_short_losing_position_does_not_scale_out():
    # a SHORT under water (price UP) must not touch the L4 target logic
    pos = make_pos("SHORT")
    bar = {"ts": None, "open": 100.0, "high": 103.0, "low": 99.8,
           "close": 102.5}
    actions = evaluate_on_bar(pos, bar, 0)
    assert not [a for a in actions if a.kind == "SCALE_OUT"]


def test_realized_move_fraction_signs():
    assert realized_move_fraction(95.0, 100.0, 0.10, "SHORT") == \
        pytest.approx(0.5)
    assert realized_move_fraction(105.0, 100.0, 0.10, "SHORT") == \
        pytest.approx(-0.5)
    assert realized_move_fraction(105.0, 100.0, 0.10, "LONG") == \
        pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 5. FakeBroker short mechanics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fake_broker_sell_from_flat_opens_short():
    b = FakeBroker()
    await b.submit_limit("ACME", "SELL", 10, 100.0, client_order_id="s1")
    pos = b.positions["ACME"]
    assert (pos.side, pos.qty, pos.avg_entry) == ("SHORT", 10, 100.0)
    # cover half
    await b.submit_limit("ACME", "BUY", 5, 95.0, client_order_id="c1")
    pos = b.positions["ACME"]
    assert (pos.side, pos.qty) == ("SHORT", 5)
    # cover the rest -> flat
    await b.submit_limit("ACME", "BUY", 5, 95.0, client_order_id="c2")
    assert "ACME" not in b.positions


@pytest.mark.asyncio
async def test_fake_broker_flip_through_zero_resets_basis():
    b = FakeBroker()
    await b.submit_limit("ACME", "BUY", 5, 100.0, client_order_id="l1")
    await b.submit_limit("ACME", "SELL", 8, 102.0, client_order_id="f1")
    pos = b.positions["ACME"]
    assert (pos.side, pos.qty, pos.avg_entry) == ("SHORT", 3, 102.0)


@pytest.mark.asyncio
async def test_fake_broker_account_reports_margin_fields():
    b = FakeBroker(equity=100_000.0)
    a = await b.get_account()
    assert a.regt_buying_power == 200_000.0
    assert a.shorting_enabled is True
