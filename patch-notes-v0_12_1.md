"""C10 candidate filters + scoring (v0.12.1) — pure functions, no I/O.

Doctrine: the scanner is deterministic code. It FINDS candidates; A2 writes
the thesis; C3 gates; A3 sizes; C4 owns exits. Every reject carries a code
that lands in journal.scanner_candidates so A9 can tune thresholds from
evidence instead of vibes.

Reject codes (FILTERED): PRICE_FLOOR, DOLLAR_VOLUME, MOVE_PCT, REL_VOLUME,
MOVE_STALE_HOD, SPREAD, LULD_HEADROOM, ETF_EXCLUDED, EARNINGS_SOON, NO_TAPE.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Best-effort ETF/ETN exclusion without a reference-data source: the heavily
# traded index/sector products that dominate movers lists on volatile days.
KNOWN_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "IVV", "RSP",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE",
    "XLC", "SMH", "SOXX", "ARKK", "KRE", "XBI", "IBB", "GDX", "GDXJ",
    "TQQQ", "SQQQ", "SPXL", "SPXS", "SOXL", "SOXS", "UVXY", "VXX", "SVXY",
    "TLT", "HYG", "LQD", "EEM", "EFA", "FXI", "EWZ", "USO", "UNG", "GLD",
    "SLV", "BITO", "IBIT", "FBTC",
}


@dataclass
class CandidateMetrics:
    """Everything measured about a candidate — journaled verbatim either way."""
    ticker: str
    price: float
    prev_close: Optional[float]
    move_pct: Optional[float]           # vs prev close (0.062 = +6.2%)
    adv20_dollars: Optional[float]
    rel_volume: Optional[float]         # day pace vs ADV(20)
    minutes_since_hod: Optional[int]
    spread_bps: Optional[float]
    luld_headroom_pct: Optional[float]  # approx: distance to +10% band from 5-min ref
    vwap: Optional[float]
    day_high: Optional[float]
    detected_ts: str = ""

    def payload(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def luld_headroom(last: float, ref_price_5m: Optional[float]) -> Optional[float]:
    """Approximate LULD up-band headroom. Tier-1 RTH band is ±5% and Tier-2
    ±10% of the 5-min reference price; without a security-tier feed we use
    the WIDER 10% band and let min_luld_headroom_pct provide the margin.
    Honest approximation, journaled as such."""
    if not ref_price_5m or not last or last <= 0:
        return None
    band_up = ref_price_5m * 1.10
    return round(max(band_up - last, 0.0) / last, 4)


def filter_candidate(m: CandidateMetrics, cfg: dict,
                     is_etf: Optional[bool] = None,
                     earnings_next_sessions: Optional[int] = None
                     ) -> Optional[str]:
    """First failing filter's reject code, or None = candidate passes.
    Null-safe: a metric that could not be computed fails CLOSED (the scanner
    proposes trades — missing evidence means no proposal; contrast with the
    TA context pack where null just means 'unavailable')."""
    if (is_etf if is_etf is not None else m.ticker in KNOWN_ETFS) \
            and cfg.get("exclude_etfs", True):
        return "ETF_EXCLUDED"
    if m.price is None or m.price < float(cfg["min_price"]):
        return "PRICE_FLOOR"
    if not m.adv20_dollars or m.adv20_dollars < float(cfg["min_adv20_dollars"]):
        return "DOLLAR_VOLUME"
    if m.move_pct is None or m.move_pct < float(cfg["min_move_pct"]):
        return "MOVE_PCT"
    if m.rel_volume is None:
        return "NO_TAPE"
    if m.rel_volume < float(cfg["min_rel_volume"]):
        return "REL_VOLUME"
    if m.minutes_since_hod is None \
            or m.minutes_since_hod > int(cfg["max_minutes_since_hod"]):
        return "MOVE_STALE_HOD"
    if m.spread_bps is None or m.spread_bps > float(cfg["max_spread_bps"]):
        return "SPREAD"
    if m.luld_headroom_pct is not None \
            and m.luld_headroom_pct < float(cfg["min_luld_headroom_pct"]):
        return "LULD_HEADROOM"
    if earnings_next_sessions is not None \
            and earnings_next_sessions <= int(cfg["earnings_blackout_sessions"]):
        return "EARNINGS_SOON"
    return None


def score_candidate(m: CandidateMetrics) -> float:
    """Composite ranking score for the top-N-per-scan cap. Weights are
    deliberately simple (rel-volume dominant — volume is the honest signal);
    A9 owns refinement."""
    rel = min(m.rel_volume or 0.0, 10.0) / 10.0          # 0..1
    move = min(m.move_pct or 0.0, 0.15) / 0.15           # 0..1
    fresh = 1.0 - min(m.minutes_since_hod or 60, 60) / 60.0
    spread = 1.0 - min(m.spread_bps or 40.0, 40.0) / 40.0
    return round(0.45 * rel + 0.30 * move + 0.15 * fresh + 0.10 * spread, 4)


def scanner_headline(m: CandidateMetrics, news_match: str) -> str:
    """The synthetic item's headline — honest about what this signal is."""
    tag = {"none": "no news match",
           "weak": "peer/sector headlines only",
           "strong": "news match"}.get(news_match, news_match)
    return (f"SCANNER: {m.ticker} +{(m.move_pct or 0) * 100:.1f}% on "
            f"{m.rel_volume:.1f}x relative volume — {tag}")


def in_scan_window(now_et_hhmm: str, cfg: dict) -> bool:
    return cfg["session_start_et"] <= now_et_hhmm < cfg["session_end_et"]
