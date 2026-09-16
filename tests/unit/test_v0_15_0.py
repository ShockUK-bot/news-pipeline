"""v0.15.0 — C12 burst stream: book arithmetic, detector rules, forward path,
config pins. Pure; no DB, no websocket."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from c12_burst.book import BUCKET_SECS, SymbolBook
from c12_burst.detect import evaluate, in_session
from c12_burst.service import _parse_ts

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[2] / "config"
DET = {"session_start_ct": "08:36", "session_end_ct": "14:55", "stale_secs": 30,
       "ret_60s": 0.004, "ret_120s": 0.006, "vol_window_secs": 60, "baseline_secs": 1800,
       "baseline_rth_only": False, "min_baseline_buckets": 24, "vol_mult": 4.0,
       "extreme_secs": 1800, "require_extreme": True, "cooldown_secs": 900}


def t0():
    return datetime(2026, 9, 16, 9, 0, tzinfo=CT).timestamp()   # 09:00 CT


def flat_book(symbol="ACME", px=100.0, secs=1800, vol=100.0, start=None):
    """One trade per bucket at a flat price for `secs` seconds."""
    b = SymbolBook(symbol)
    s = (start if start is not None else t0()) - secs
    for i in range(secs // BUCKET_SECS):
        b.add_trade(s + i * BUCKET_SECS + 1, px, vol)
    return b


# --- book ---------------------------------------------------------------------

def test_buckets_aggregate_and_roll():
    b = SymbolBook("A", keep_secs=60)
    base = 1_000_000.0
    for i in range(20):                      # 20 buckets of 5 s -> keep 12
        b.add_trade(base + i * 5, 10 + i, 1)
    assert len(b.buckets) == 12
    assert b.last_price == 29
    assert b.price_at(base + 19 * 5) == 29


def test_ret_and_extreme():
    b = flat_book()
    now = t0()
    b.add_trade(now, 100.6, 500)            # +0.6% print
    assert round(b.ret(now, 60), 4) == 0.006
    hi, lo = b.extreme(now, 1800)           # excludes the current bucket
    assert hi == 100.0 and lo == 100.0


def test_vol_mult_uses_median_baseline():
    b = flat_book(vol=100.0)
    now = t0()
    for k in range(12):                     # 60 s window at 5x pace
        b.add_trade(now - 59 + k * 5, 100.0, 500.0)
    vm = b.vol_mult(now, 60, 1800, 24)
    assert 4.5 < vm <= 5.5


def test_forward_path_scores_target_and_stop():
    b = flat_book()
    now = t0()
    px = 100.0
    # +1.2% at +3 min, then back
    b.add_trade(now + 180, 101.2, 10)
    b.add_trade(now + 600, 100.5, 10)
    b.add_trade(now + 1800, 100.4, 10)
    p = b.path(now, px, +1, 1800, 0.01, 0.007)
    assert p["first_hit"] == "target" and p["first_hit_min"] == 3.0
    assert p["p_30m"] == 100.4 and p["max_fav_pct"] >= 0.012
    # short from 100 that goes against us first
    p2 = b.path(now, px, -1, 1800, 0.01, 0.007)
    assert p2["first_hit"] == "stop"


# --- detector -------------------------------------------------------------------

def burst(b, now, pct=0.006, vol=600.0):
    for k in range(12):
        b.add_trade(now - 59 + k * 5, 100.0 * (1 + pct * (k + 1) / 12), vol)


def test_momentum_fires_on_return_volume_and_extreme():
    b = flat_book()
    now = t0()
    burst(b, now)
    ev = evaluate(b, now, DET, {})
    assert ev is not None and ev.rule == "momentum" and ev.direction == 1
    assert ev.vol_mult >= 4 and ev.new_extreme


def test_short_burst_fires_down():
    b = flat_book()
    now = t0()
    burst(b, now, pct=-0.006)
    ev = evaluate(b, now, DET, {})
    assert ev is not None and ev.direction == -1


def test_low_volume_burst_is_ignored():
    b = flat_book()
    now = t0()
    burst(b, now, vol=150.0)                # 1.5x pace only
    assert evaluate(b, now, DET, {}) is None


def test_burst_without_new_extreme_is_ignored():
    b = flat_book()
    now = t0()
    b.add_trade(now - 900, 101.5, 100)      # earlier high above the burst
    burst(b, now)
    assert evaluate(b, now, DET, {}) is None
    assert evaluate(b, now, {**DET, "require_extreme": False}, {}) is not None


def test_cooldown_blocks_repeat():
    b = flat_book()
    now = t0()
    burst(b, now)
    fired = {}
    assert evaluate(b, now, DET, fired) is not None
    burst(b, now + 60, pct=0.006)           # still bursting a minute later
    assert evaluate(b, now + 60, DET, fired) is None      # cooldown
    later = now + 901
    t = now + 65
    while t < later - 65:                   # quiet prints at the base price keep
        b.add_trade(t, 100.0, 100.0)        # the symbol fresh until the next burst
        t += 5
    burst(b, later, pct=0.006)              # a fresh burst after the cooldown
    assert evaluate(b, later, DET, fired) is not None


def test_stale_symbol_is_skipped():
    b = flat_book()
    now = t0()
    burst(b, now)
    assert evaluate(b, now + 120, DET, {}) is None


def test_session_window():
    assert in_session(datetime(2026, 9, 16, 9, 0, tzinfo=CT).timestamp(), DET)
    assert not in_session(datetime(2026, 9, 16, 8, 31, tzinfo=CT).timestamp(), DET)
    assert not in_session(datetime(2026, 9, 16, 15, 5, tzinfo=CT).timestamp(), DET)
    assert not in_session(datetime(2026, 9, 19, 10, 0, tzinfo=CT).timestamp(), DET)  # Saturday


def test_parse_alpaca_timestamps():
    assert _parse_ts("2026-09-16T14:00:00.123456789Z") is not None
    assert _parse_ts("2026-09-16T14:00:00Z") is not None
    assert _parse_ts(None) is None


# --- config pins ------------------------------------------------------------------

def test_burst_yaml_pins():
    cfg = yaml.safe_load((ROOT / "burst.yaml").read_text())
    d = cfg["detect"]
    assert d["ret_60s"] > 0 and d["ret_120s"] > d["ret_60s"]
    assert d["vol_mult"] >= 2 and d["cooldown_secs"] >= 300
    assert d["horizon_secs"] <= cfg["keep_secs"] - 300, "book must outlive the horizon"
    assert cfg["universe"]["max_symbols"] <= 500
    assert "08:3" in d["session_start_ct"] and d["session_end_ct"] <= "14:55"


def test_c12_has_no_order_path():
    src = (Path(__file__).resolve().parents[2] / "src" / "c12_burst" / "service.py").read_text()
    assert "enqueue(" not in src and "exec.intent" not in src and "submit_limit" not in src
