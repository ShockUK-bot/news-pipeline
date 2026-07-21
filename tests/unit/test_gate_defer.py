"""v0.11.10 unit tests: C3 defer-until-mature arithmetic (no DB).

Incident (2026-07-21): fast Alpaca-news items reached C3 ~60s after publish.
The since-news window [published_ts, now] cannot contain a single COMPLETED
minute bar until the minute after the publish minute closes, so
minute_bars() returned [] for ANY ticker (GM at 10:00 ET, EMBJ at 11:10 ET),
avg_minute_volume() -> None, and the gate terminally vetoed
MARKETDATA_MISSING — killing exactly the fast in-session signals a
news-momentum system wants. These tests pin the maturity arithmetic and the
defer-delay floor/cap; the queue round trip is covered in
tests/integration/test_analyst_gate_flow.py (test_10/test_11).
"""
from datetime import datetime, timezone

import pytest

from c3_gate.service import bars_mature_ts, defer_delay


def T(h, m, s=0):
    return datetime(2026, 7, 21, h, m, s, tzinfo=timezone.utc)


# ---- bars_mature_ts -----------------------------------------------------------

def test_mature_ts_mid_minute_publish():
    # published 14:00:12 -> first coverable bar is stamped 14:01 and completes
    # at 14:02; three bars (14:01, 14:02, 14:03) all complete at 14:04:00.
    assert bars_mature_ts(T(14, 0, 12), 3) == T(14, 4)


def test_mature_ts_on_exact_boundary_is_conservative():
    # boundary-published news could count its own minute's bar, but the
    # implementation deliberately ignores it (one minute conservative).
    assert bars_mature_ts(T(14, 0, 0), 3) == T(14, 4)


def test_mature_ts_scales_with_min_bars():
    assert bars_mature_ts(T(14, 0, 12), 1) == T(14, 2)
    assert bars_mature_ts(T(14, 0, 12), 5) == T(14, 6)


# ---- defer_delay --------------------------------------------------------------

def test_incident_shape_60s_lag_defers():
    # GM alpaca:60578634 exactly: published 13:59:32Z, evaluated 14:00:36Z.
    # mature = 14:03:00 -> defer 144s + 1s guard.
    d = defer_delay(T(13, 59, 32), T(14, 0, 36), 3)
    assert d == pytest.approx(145.0)


def test_mature_window_returns_none():
    assert defer_delay(T(14, 0, 12), T(14, 4, 0), 3) is None       # exactly mature
    assert defer_delay(T(14, 0, 12), T(14, 30, 0), 3) is None      # long past


def test_floor_prevents_busy_reclaim_loop():
    # 2s short of maturity -> floored to 5s, never a sub-second re-claim spin.
    assert defer_delay(T(14, 0, 12), T(14, 3, 58), 3) == 5.0


def test_future_skewed_publish_is_capped():
    # a feed timestamp an hour in the future must not park the message for
    # hours; capped defers repeat harmlessly (defer refunds the attempt).
    assert defer_delay(T(15, 0, 0), T(14, 0, 0), 3) == 300.0
