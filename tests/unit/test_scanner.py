"""v0.12.1 unit tests — C10 scanner rules + C3 scanner gate branch.

Pure functions only (the service layers are exercised on the Spark's
integration environment). The doctrine under test: the scanner PROPOSES, so
missing evidence fails CLOSED everywhere in this lane.
"""
import pytest

from c10_scanner.rules import (CandidateMetrics, filter_candidate,
                               in_scan_window, luld_headroom,
                               scanner_headline, score_candidate)
from c3_gate.rules import ScannerState, evaluate_scanner

CFG = {"min_price": 5.0, "min_adv20_dollars": 25_000_000,
       "min_move_pct": 0.04, "min_rel_volume": 3.0,
       "max_minutes_since_hod": 60, "max_spread_bps": 40,
       "min_luld_headroom_pct": 0.02, "exclude_etfs": True,
       "earnings_blackout_sessions": 1,
       "session_start_et": "09:50", "session_end_et": "15:15"}

GCFG = {"stale_max_min": 6, "stale_run_pct": 0.015,
        "require_above_vwap": True, "range30_min_pos": 0.5,
        "parabolic_bar_ratio": 2.5, "max_spread_bps": 40}


def metrics(**over):
    m = CandidateMetrics(
        ticker="MU", price=120.0, prev_close=113.0, move_pct=0.062,
        adv20_dollars=900_000_000.0, rel_volume=4.1, minutes_since_hod=12,
        spread_bps=6.0, luld_headroom_pct=0.09, vwap=118.4,
        day_high=120.6, detected_ts="2026-07-23T15:00:00+00:00")
    for k, v in over.items():
        setattr(m, k, v)
    return m


# ---- C10 filters -------------------------------------------------------------

def test_clean_candidate_passes():
    assert filter_candidate(metrics(), CFG) is None


@pytest.mark.parametrize("over,code", [
    ({"price": 4.5}, "PRICE_FLOOR"),
    ({"adv20_dollars": 5_000_000.0}, "DOLLAR_VOLUME"),
    ({"adv20_dollars": None}, "DOLLAR_VOLUME"),
    ({"move_pct": 0.02}, "MOVE_PCT"),
    ({"move_pct": None}, "MOVE_PCT"),
    ({"rel_volume": 1.4}, "REL_VOLUME"),
    ({"rel_volume": None}, "NO_TAPE"),
    ({"minutes_since_hod": 90}, "MOVE_STALE_HOD"),
    ({"minutes_since_hod": None}, "MOVE_STALE_HOD"),
    ({"spread_bps": 55.0}, "SPREAD"),
    ({"spread_bps": None}, "SPREAD"),
    ({"luld_headroom_pct": 0.01}, "LULD_HEADROOM"),
])
def test_filter_matrix_fails_closed(over, code):
    assert filter_candidate(metrics(**over), CFG) == code


def test_etf_excluded_and_earnings_blackout():
    assert filter_candidate(metrics(ticker="TQQQ"), CFG) == "ETF_EXCLUDED"
    assert filter_candidate(metrics(), CFG,
                            earnings_next_sessions=0) == "EARNINGS_SOON"
    assert filter_candidate(metrics(), CFG,
                            earnings_next_sessions=1) == "EARNINGS_SOON"
    assert filter_candidate(metrics(), CFG,
                            earnings_next_sessions=5) is None


def test_luld_headroom_unknown_is_allowed_but_null_metrics_are_not():
    # LULD approximation can be unavailable (no 5-min ref yet) — it is the
    # ONE null we allow through, because the gate re-checks liquidity NOW.
    assert filter_candidate(metrics(luld_headroom_pct=None), CFG) is None


def test_score_prefers_volume_then_move():
    hi_vol = score_candidate(metrics(rel_volume=8.0))
    lo_vol = score_candidate(metrics(rel_volume=3.0))
    assert hi_vol > lo_vol
    big_move = score_candidate(metrics(move_pct=0.12))
    small_move = score_candidate(metrics(move_pct=0.045))
    assert big_move > small_move


def test_luld_headroom_math():
    # ref 100 -> band 110; last 104 -> 5.77% headroom
    assert luld_headroom(104.0, 100.0) == pytest.approx(0.0577, abs=0.001)
    assert luld_headroom(104.0, None) is None
    assert luld_headroom(115.0, 100.0) == 0.0          # above band -> zero


def test_headline_is_honest():
    h = scanner_headline(metrics(), "none")
    assert "SCANNER: MU" in h and "no news match" in h
    assert "peer/sector" in scanner_headline(metrics(), "weak")


def test_scan_window():
    assert not in_scan_window("09:49", CFG)
    assert in_scan_window("09:50", CFG)
    assert in_scan_window("15:14", CFG)
    assert not in_scan_window("15:15", CFG)


# ---- C3 scanner gate branch --------------------------------------------------

def sstate(**over):
    s = ScannerState(last_price=120.5, detect_price=120.0,
                     minutes_since_detect=2.0, vwap=118.4, range30_pos=0.8,
                     bar5_range_ratio=1.2, spread_bps=8.0, halted=False)
    for k, v in over.items():
        setattr(s, k, v)
    return s


UP = {"direction": "up"}


def test_scanner_gate_pass():
    v = evaluate_scanner(UP, sstate(), GCFG)
    assert v.verdict == "PASS" and v.rule == "scanner"
    assert v.numbers["run_since_detect_pct"] == pytest.approx(0.00417, abs=1e-4)


def test_scanner_gate_long_only():
    v = evaluate_scanner({"direction": "down"}, sstate(), GCFG)
    assert (v.verdict, v.veto_reason) == ("VETO", "LONG_ONLY")


@pytest.mark.parametrize("over,reason", [
    ({"minutes_since_detect": 9.0}, "SCANNER_STALE"),
    ({"last_price": 122.5}, "SCANNER_STALE"),          # ran +2.1% since detect
    ({"last_price": 118.0}, "SCANNER_STRUCTURE"),      # below VWAP
    ({"vwap": None}, "SCANNER_STRUCTURE"),             # fails closed
    ({"range30_pos": 0.3}, "SCANNER_STRUCTURE"),       # lower half of range
    ({"range30_pos": None}, "SCANNER_STRUCTURE"),
    ({"bar5_range_ratio": 3.4}, "SCANNER_PARABOLIC"),  # vertical bar
    ({"bar5_range_ratio": None}, "SCANNER_PARABOLIC"),
    ({"halted": True}, "SCANNER_LIQUIDITY"),
    ({"spread_bps": 70.0}, "SCANNER_LIQUIDITY"),
    ({"spread_bps": None}, "SCANNER_LIQUIDITY"),
])
def test_scanner_gate_veto_matrix(over, reason):
    v = evaluate_scanner(UP, sstate(**over), GCFG)
    assert (v.verdict, v.veto_reason) == ("VETO", reason)


def test_scanner_gate_numbers_journal_everything():
    v = evaluate_scanner(UP, sstate(minutes_since_detect=9.0), GCFG)
    for key in ("last", "detect_price", "run_since_detect_pct",
                "minutes_since_detect", "vwap", "range30_pos",
                "bar5_range_ratio", "spread_bps", "halted"):
        assert key in v.numbers
