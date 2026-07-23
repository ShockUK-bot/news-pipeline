"""v0.12.0 unit tests — TA context pack (common/ta.py) + clock.session_open.

Pure functions + FakeData; no DB. The doctrine under test everywhere:
null-safe degradation (thin history / off-session / provider failure -> None
fields with a STABLE key shape), never an exception, never a fake number.
"""
from datetime import datetime, timedelta, timezone

import pytest

from common.clock import session_open
from common.marketdata import FakeData, Quote
from common.ta import (NULL_DAILY, NULL_INTRADAY, atr_5m, build_ta_pack,
                       day_range_pos, day_vwap, dist_from_high, resample_5m,
                       ret_nd_pct, rel_volume_day, rsi14, sma_dist_pct,
                       trend_20_50)

UTC = timezone.utc


def daily_ramp(n, start=100.0, step=1.0, volume=1_000_000):
    """n daily bars closing start, start+step, ..."""
    out = []
    for i in range(n):
        c = start + step * i
        out.append({"ts": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i),
                    "open": c - step / 2, "high": c + 0.5, "low": c - 1.0,
                    "close": c, "volume": volume, "vwap": c})
    return out


# ---- session_open ------------------------------------------------------------

def test_session_open_weekday_is_930_et():
    # 2026-07-22 is a Wednesday; 9:30 ET (EDT) == 13:30 UTC
    dt = datetime(2026, 7, 22, 18, 0, tzinfo=UTC)
    assert session_open(dt) == datetime(2026, 7, 22, 13, 30, tzinfo=UTC)


def test_session_open_weekend_is_none():
    assert session_open(datetime(2026, 7, 25, 15, 0, tzinfo=UTC)) is None  # Sat


# ---- daily indicators --------------------------------------------------------

def test_rsi_needs_15_bars():
    assert rsi14(daily_ramp(14)) is None


def test_rsi_all_gains_is_100():
    assert rsi14(daily_ramp(40, step=1.0)) == 100.0


def test_rsi_all_losses_is_low():
    assert rsi14(daily_ramp(40, step=-1.0)) < 1.0


def test_rsi_flat_tape_is_midrange():
    bars = daily_ramp(40, step=0.0)
    # zero gains, zero losses -> avg_l == 0 -> 100 by convention; nudge one
    bars[20]["close"] += 1.0
    v = rsi14(bars)
    assert v is not None and 0.0 <= v <= 100.0


def test_sma_dist_and_trend():
    up = daily_ramp(60, step=1.0)
    last = up[-1]["close"]
    assert sma_dist_pct(up, last, 20) > 0
    assert trend_20_50(up) == "up"
    down = daily_ramp(60, start=200.0, step=-1.0)
    assert trend_20_50(down) == "down"
    assert trend_20_50(daily_ramp(30)) is None          # < 50 bars


def test_dist_from_high_honest_window():
    bars = daily_ramp(100, step=1.0)
    dist, win = dist_from_high(bars, bars[-1]["close"])
    assert win == 100                                    # honest: what we have
    assert dist == pytest.approx(-0.25, abs=0.05)        # 0.5 below today's high
    assert dist_from_high(daily_ramp(10), 100.0) == (None, None)


def test_ret_5d():
    assert ret_nd_pct(daily_ramp(30, start=100, step=1.0), 5) == pytest.approx(
        (129.0 / 124.0 - 1) * 100, abs=0.01)
    assert ret_nd_pct(daily_ramp(4), 5) is None


# ---- intraday indicators -----------------------------------------------------

def minute_session(start, minutes, price=100.0, vol=10_000):
    return FakeData.ramp_minute(start, minutes, price, price, vol)


def test_resample_excludes_in_progress_bucket():
    start = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
    now = start + timedelta(minutes=32)                  # in the 14:00 bucket
    bars5 = resample_5m(minute_session(start, 32), now)
    assert len(bars5) == 6                               # 13:30..13:55 complete
    assert all(b["volume"] == 50_000 for b in bars5)


def test_atr5m_needs_75_minutes():
    start = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
    now = start + timedelta(minutes=60)
    assert atr_5m(minute_session(start, 60), now) is None      # 11 buckets
    now = start + timedelta(minutes=80)
    assert atr_5m(minute_session(start, 80), now) is not None  # 16 buckets


def test_day_vwap_and_range_pos():
    start = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
    bars = FakeData.ramp_minute(start, 30, 100.0, 110.0, 10_000)
    v = day_vwap(bars)
    assert 100.0 < v < 110.0
    assert day_range_pos(bars, 110.0) == 1.0
    assert day_range_pos(bars, 100.0) == 0.0
    assert day_range_pos([], 100.0) is None


def test_rel_volume_day():
    start = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
    daily = daily_ramp(25, volume=390 * 10_000)          # ADV = 3.9M
    bars = minute_session(start, 60)                     # 600k in 60 min
    assert rel_volume_day(bars, daily, 60) == pytest.approx(1.0, abs=0.01)
    fast = minute_session(start, 60, vol=40_000)         # 4x pace
    assert rel_volume_day(fast, daily, 60) == pytest.approx(4.0, abs=0.05)
    assert rel_volume_day(bars, daily, 10) is None       # too early
    assert rel_volume_day(bars, daily_ramp(5), 60) is None  # no ADV20


# ---- pack builder ------------------------------------------------------------

async def test_pack_shape_is_stable_even_with_no_data():
    class DeadData:
        async def minute_bars(self, *a): raise RuntimeError("down")
        async def daily_bars(self, *a): raise RuntimeError("down")
        async def snapshot(self, *a): raise RuntimeError("down")
        async def prev_close(self, *a): raise RuntimeError("down")

    pack = await build_ta_pack(DeadData(), "ACME")
    assert set(pack) == {"intraday", "daily"}
    assert pack["intraday"] == NULL_INTRADAY
    assert pack["daily"] == NULL_DAILY


async def test_pack_daily_fields_populate():
    md = FakeData()
    md.set_daily("ACME", daily_ramp(260, step=0.1))
    md.set_quote("ACME", Quote(price=126.0, bid=125.98, ask=126.02,
                               ts=datetime(2026, 7, 22, 15, 0, tzinfo=UTC)))
    pack = await build_ta_pack(md, "ACME")
    d = pack["daily"]
    assert d["rsi_14"] is not None
    assert d["trend_20_50"] == "up"
    assert d["high_window_sessions"] == 252
    assert d["atr_14"] is not None
    # keys are exactly the documented shape
    assert set(d) == set(NULL_DAILY)
    assert set(pack["intraday"]) == set(NULL_INTRADAY)


async def test_pack_intraday_only_skips_daily():
    md = FakeData()
    calls = []
    orig = md.daily_bars

    async def spy(symbol, n):
        calls.append(n)
        return await orig(symbol, n)
    md.daily_bars = spy
    pack = await build_ta_pack(md, "ACME", intraday_only=True)
    assert calls == []                                   # no 260-bar fetch
    assert pack["daily"] == NULL_DAILY
    assert pack["intraday"]["rel_volume_day"] is None    # needs daily
