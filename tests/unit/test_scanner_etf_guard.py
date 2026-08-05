"""v0.12.14 unit tests — the hardened ETF exclusion and the scanner-reject
counterfactual args. Context: 2026-08-04, PTIR and PLTU (2x PLTR leveraged
ETFs) leaked through the ticker-set filter, burned 2 of 6 daily emission
slots and 2 analyst calls; the resulting A2 rejects were unmeasurable."""
from datetime import datetime, timezone
from types import SimpleNamespace

from c10_scanner.rules import (CandidateMetrics, filter_candidate,
                               looks_like_etf)
from a2_analyst.service import scanner_reject_cf_args


def _metrics(ticker="ACME", **over):
    base = dict(ticker=ticker, price=20.0, prev_close=18.0, move_pct=0.11,
                adv20_dollars=60_000_000, rel_volume=6.0,
                minutes_since_hod=10, spread_bps=12.0,
                luld_headroom_pct=0.06, vwap=19.5, day_high=20.4)
    base.update(over)
    return CandidateMetrics(**base)


CFG = {"min_price": 5.0, "min_adv20_dollars": 25_000_000,
       "min_move_pct": 0.04, "min_rel_volume": 3.0,
       "max_minutes_since_hod": 60, "max_spread_bps": 40,
       "min_luld_headroom_pct": 0.02, "exclude_etfs": True,
       "earnings_blackout_sessions": 1}


# --- name-based detection (the 2026-08-04 leakers, verbatim) ---------------

def test_name_catches_the_actual_leakers():
    assert looks_like_etf("GraniteShares 2x Long PLTR Daily ETF") is True
    assert looks_like_etf("Direxion Daily PLTR Bull 2X Shares") is True


def test_name_catches_generic_etf_forms():
    assert looks_like_etf("SPDR S&P 500 ETF Trust") is True
    assert looks_like_etf("iPath Series B S&P 500 VIX ETN") is True
    assert looks_like_etf("ProShares UltraPro QQQ") is True          # issuer
    assert looks_like_etf("Tuttle Capital 2X Inverse Regional Banks") is True
    assert looks_like_etf("Gabelli Dividend & Income Fund") is True  # fund word


def test_name_leaves_operating_companies_alone():
    assert looks_like_etf("Palantir Technologies Inc. Class A") is False
    assert looks_like_etf("Build-A-Bear Workshop, Inc.") is False    # Bear!
    assert looks_like_etf("United States Steel Corporation") is False
    assert looks_like_etf("SpaceX Holdings") is False                # X not nX
    assert looks_like_etf(None) is False
    assert looks_like_etf("") is False


# --- filter integration ----------------------------------------------------

def test_filter_excludes_by_asset_name():
    m = _metrics(ticker="PTIR")   # also in KNOWN_ETFS now, so test a fresh one
    m2 = _metrics(ticker="ZZZL")
    assert filter_candidate(
        m2, CFG, asset_name="GraniteShares 2x Long ZZZ Daily ETF",
        earnings_next_sessions=5) == "ETF_EXCLUDED"
    assert filter_candidate(m, CFG, earnings_next_sessions=5) == "ETF_EXCLUDED"


def test_filter_excludes_by_operator_denylist():
    cfg = {**CFG, "etf_denylist": ["zzzl"]}
    assert filter_candidate(_metrics(ticker="ZZZL"), cfg,
                            earnings_next_sessions=5) == "ETF_EXCLUDED"


def test_filter_passes_common_stock_with_name():
    m = _metrics(ticker="ACME")
    assert filter_candidate(m, CFG, asset_name="Acme Industries, Inc.",
                            earnings_next_sessions=5) is None


def test_exclude_etfs_false_disables_all_layers():
    cfg = {**CFG, "exclude_etfs": False, "etf_denylist": ["SPY"]}
    m = _metrics(ticker="SPY")
    assert filter_candidate(m, cfg, asset_name="SPDR S&P 500 ETF Trust",
                            earnings_next_sessions=5) is None


# --- scanner-reject counterfactual args ------------------------------------

def test_cf_args_map_snapshot_to_record_veto_fields():
    thesis = SimpleNamespace(ticker="AAOX", direction="down")
    scanner = {"price": 12.4, "prev_close": 9.21, "move_pct": 0.3463,
               "rel_volume": 8.33, "score": 0.7996}
    ts = datetime(2026, 8, 4, 13, 54, tzinfo=timezone.utc)
    args = scanner_reject_cf_args(decision_id=999, signal_id="scanner:AAOX:d",
                                  item_id="scanner:AAOX:d", thesis=thesis,
                                  scanner=scanner, veto_ts=ts)
    assert args["rule"] == "scanner_reject"
    assert args["veto_reason"] == "ANALYST_REJECT"
    assert args["ticker"] == "AAOX" and args["direction"] == "down"
    assert args["price_at_veto"] == 12.4
    assert args["prenews_price"] == 9.21
    assert args["pct_move"] == 0.3463 and args["vol_mult"] == 8.33
    assert args["veto_ts"] is ts and args["decision_id"] == 999


def test_cf_args_tolerate_missing_snapshot_fields():
    thesis = SimpleNamespace(ticker="XYZ", direction="up")
    args = scanner_reject_cf_args(decision_id=1, signal_id="s", item_id=None,
                                  thesis=thesis, scanner={"score": 0.5},
                                  veto_ts=datetime(2026, 8, 4,
                                                   tzinfo=timezone.utc))
    assert args["price_at_veto"] is None and args["vol_mult"] is None
