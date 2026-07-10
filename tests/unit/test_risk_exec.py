"""Phase 4 unit tests: the A3 sizing chain (every veto, every clip, the
viability rule), discretion band validation, limit pricing, FakeBroker
semantics. No DB."""
import asyncio
import json

import pytest

from a3_risk.sizing import (SizingInputs, limit_price_from_snapshot,
                            open_risk_dollars, size_entry)
from a3_risk.service import validate_adjustments
from common.broker import BrokerReject, FakeBroker

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
BANDS = {"k": [1.5, 2.5], "realization_fraction": [0.5, 0.9],
         "time_window_sessions": [1, 3]}


def inputs(**over):
    base = dict(effective_capital=50_000.0, settled_cash=50_000.0,
                ref_price=100.0, bid=99.98, ask=100.02, spread_bps=4.0,
                atr_14=1.5, adv_20d=5_000_000.0,
                open_heat={"SHORT": 0.0, "LONG": 0.0},
                deployed_notional=0.0, trades_today=0,
                minutes_to_close=120)
    base.update(over)
    return SizingInputs(**base)


def run(inp, k=2.0, horizon="SHORT"):
    return size_entry(inp, CAPITAL, LIMITS, PROFILE, horizon, k)


# ---- the happy path ---------------------------------------------------------

def test_clean_size():
    # ATR 2.0 -> stop distance 4.0 -> 62 shares, no clip binds
    r = run(inputs(atr_14=2.0))
    assert r.verdict == "SIZE" and r.qty == 62
    assert r.risk_budget == 250.0
    assert r.actual_risk == pytest.approx(248.0)
    assert r.initial_stop == pytest.approx(r.limit_price - 4.0, abs=0.01)
    assert r.catastrophe_stop == pytest.approx(r.limit_price - 7.0, abs=0.01)
    assert r.numbers["binding_clip"] is None
    assert "EARNINGS_UNKNOWN" in r.flags and "SECTOR_UNKNOWN" in r.flags


def test_default_fixture_notional_trim_is_viable():
    # At ATR=1.5%-of-price the risk-derived size is ~16.7% notional; the 15%
    # cap trims it slightly and the trade stays viable (design observation).
    r = run(inputs())
    assert r.verdict == "SIZE" and r.numbers["binding_clip"] == "notional"
    assert r.actual_risk >= 0.85 * r.risk_budget


def test_limit_price_buffer_capped_at_10bps():
    # spread 4bps -> half-spread 2bps buffer
    assert limit_price_from_snapshot(100.0, 4.0) == pytest.approx(100.02)
    # spread 40bps -> half is 20bps but cap at 10bps
    assert limit_price_from_snapshot(100.0, 40.0) == pytest.approx(100.10)


# ---- hard-gate vetoes -------------------------------------------------------

@pytest.mark.parametrize("field,value,reason", [
    ("kill_switch", True, "KILL_SWITCH"),
    ("breaker", True, "BREAKER"),
    ("block_entries", True, "BLOCK_ENTRIES"),
    ("ticker_halted", True, "HALTED"),
    ("trades_today", 5, "MAX_TRADES"),
    ("minutes_to_close", 10, "ENTRY_BLACKOUT"),
    ("spread_bps", 55.0, "WIDE_SPREAD"),
    ("atr_14", None, "NO_ATR"),
])
def test_hard_gate_vetoes(field, value, reason):
    r = run(inputs(**{field: value}))
    assert (r.verdict, r.veto_reason) == ("VETO", reason)


def test_earnings_blackout_when_known():
    r = run(inputs(earnings_next_sessions=1))
    assert r.veto_reason == "EARNINGS_BLACKOUT"


def test_earnings_unknown_allows_with_flag():
    r = run(inputs(earnings_next_sessions=None))
    assert r.verdict == "SIZE" and "EARNINGS_UNKNOWN" in r.flags


def test_max_trades_respects_operational_control():
    r = size_entry(inputs(trades_today=7, max_trades_per_day=10),
                   CAPITAL, LIMITS, PROFILE, "SHORT", 2.0)
    assert r.verdict == "SIZE"          # dashboard raised the throttle


# ---- clips ------------------------------------------------------------------

def test_notional_clip_then_viability_veto():
    # tiny ATR -> huge raw qty -> 15% notional cap binds -> clipped risk is
    # trivial vs intended -> the viability rule correctly kills the trade
    r = run(inputs(atr_14=0.05))
    assert (r.verdict, r.veto_reason) == ("VETO", "SIZE_CLIPPED")
    assert r.numbers["binding_clip"] == "notional"
    assert r.numbers["qty"] == 74                # 7500 / 100.02ish
    assert r.numbers["actual_risk"] < 0.5 * r.numbers["risk_budget"]


def test_adv_clip_binds():
    r = run(inputs(atr_14=0.05, adv_20d=5_000))
    assert r.numbers["binding_clip"] == "adv"
    assert r.numbers["qty"] == 50                # 1% of 5000 ADV
    assert r.veto_reason == "SIZE_CLIPPED"       # and viability kills it


def test_settled_cash_clip():
    r = run(inputs(atr_14=0.05, settled_cash=2_000.0))
    assert r.numbers["binding_clip"] == "settled_cash"


def test_lane_heat_exhaustion_clips_to_zero():
    # SHORT lane cap = 2% * 50k = 1000; already used 900 -> headroom 100/3.0=33
    r = run(inputs(open_heat={"SHORT": 900.0, "LONG": 0.0}))
    assert r.verdict == "VETO" and r.veto_reason == "SIZE_CLIPPED"
    assert r.numbers["binding_clip"] == "lane_heat"


def test_total_heat_counts_both_lanes():
    # total cap 1500; long lane holds 1290 -> total headroom 210 -> 70 shares,
    # tighter than the 74.97-share notional cap; still viable (210 >= 125)
    r = run(inputs(open_heat={"SHORT": 0.0, "LONG": 1290.0}))
    assert r.verdict == "SIZE"
    assert r.numbers["binding_clip"] == "total_heat"
    assert r.qty == 70


def test_capital_headroom_preflight_clip():
    r = run(inputs(deployed_notional=49_000.0, atr_14=0.05))
    assert r.numbers["binding_clip"] == "capital_headroom"


def test_size_clipped_viability():
    # heat leaves 100/3.0 = 33 shares = $99 actual vs $250 intended -> <50%
    r = run(inputs(open_heat={"SHORT": 900.0, "LONG": 0.0}))
    assert r.veto_reason == "SIZE_CLIPPED"
    assert r.numbers["actual_risk"] < 0.5 * r.numbers["risk_budget"]


def test_open_risk_house_money_is_zero():
    assert open_risk_dollars(100, 50.0, 48.0) == 200.0
    assert open_risk_dollars(100, 50.0, 51.0) == 0.0   # stop above entry


# ---- discretion bands -------------------------------------------------------

def adj_json(**over):
    base = {"k": 2.0, "realization_fraction": 0.7,
            "time_window_sessions": 2, "reason": "clean confirmation"}
    base.update(over)
    return json.dumps(base)


def test_adjustments_within_bands():
    a = validate_adjustments(adj_json(k=1.5), BANDS)
    assert a.k == 1.5


@pytest.mark.parametrize("over", [{"k": 3.0}, {"k": 1.0},
                                  {"realization_fraction": 0.95},
                                  {"time_window_sessions": 5}])
def test_adjustments_outside_bands_rejected(over):
    with pytest.raises(ValueError):
        validate_adjustments(adj_json(**over), BANDS)


# ---- FakeBroker semantics ----------------------------------------------------

def test_fakebroker_fill_and_position():
    async def go():
        b = FakeBroker(settled_cash=10_000)
        o = await b.submit_limit("ACME", "BUY", 50, 100.0, "coid-1")
        assert o.status == "filled" and o.filled_avg_price == 100.0
        pos = await b.get_positions()
        assert pos[0].qty == 50
        acct = await b.get_account()
        assert acct.settled_cash == 5_000
    asyncio.run(go())


def test_fakebroker_idempotent_client_order_id():
    async def go():
        b = FakeBroker()
        o1 = await b.submit_limit("ACME", "BUY", 50, 100.0, "coid-x")
        o2 = await b.submit_limit("ACME", "BUY", 50, 100.0, "coid-x")
        assert o1.broker_order_id == o2.broker_order_id
        assert len(b.submissions) == 1
    asyncio.run(go())


def test_fakebroker_stop_rests_then_manual_fill():
    async def go():
        b = FakeBroker()
        await b.submit_limit("ACME", "BUY", 50, 100.0, "e1")
        s = await b.submit_stop("ACME", "SELL", 50, 95.0, "cat-e1")
        assert s.status == "accepted"
        b.fill_order(s.broker_order_id, price=94.8)
        assert (await b.get_order(s.broker_order_id)).status == "filled"
        assert await b.get_positions() == []       # flat after stop fill
    asyncio.run(go())


def test_fakebroker_reject_and_partial():
    async def go():
        b = FakeBroker()
        b.set_behavior("bad", "reject")
        with pytest.raises(BrokerReject):
            await b.submit_limit("ACME", "BUY", 10, 100.0, "bad")
        b.set_behavior("p1", "partial:20")
        o = await b.submit_limit("ACME", "BUY", 50, 100.0, "p1")
        assert o.status == "partially_filled" and o.filled_qty == 20
    asyncio.run(go())

