"""C10 candidate filters + scoring (v0.12.1) — pure functions, no I/O.

Doctrine: the scanner is deterministic code. It FINDS candidates; A2 writes
the thesis; C3 gates; A3 sizes; C4 owns exits. Every reject carries a code
that lands in journal.scanner_candidates so A9 can tune thresholds from
evidence instead of vibes.

Reject codes (FILTERED): PRICE_FLOOR, DOLLAR_VOLUME, MOVE_PCT, REL_VOLUME,
MOVE_STALE_HOD, SPREAD, LULD_HEADROOM, ETF_EXCLUDED, EARNINGS_SOON, NO_TAPE,
INSTRUMENT_SHAPE (v0.12.28 — journaled by the service before measurement,
not by filter_candidate; see looks_like_derivative below), and — v0.13.8,
emission-stage — SCORE_FLOOR, SSR_RESTRICTED, SHORT_UNAVAILABLE (see
emission_disposition below: quality/viability rejects that must NOT consume
emission budget).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Best-effort ETF/ETN exclusion without a reference-data source: the heavily
# traded index/sector products that dominate movers lists on volatile days.
# v0.12.14: extended with the single-stock leveraged wrappers that leaked
# through on 2026-08-04 (PTIR/PLTU — 2x PLTR ETFs burned 2 of the 6 daily
# emission slots and 2 analyst calls on the exact instruments the spec
# excludes). The ticker set is the FALLBACK layer; the primary check is now
# looks_like_etf() on the asset's official name (service fetches it from the
# broker's assets API and caches per day), plus the operator-editable
# scanner.etf_denylist in config.
KNOWN_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "IVV", "RSP",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE",
    "XLC", "SMH", "SOXX", "ARKK", "KRE", "XBI", "IBB", "GDX", "GDXJ",
    "TQQQ", "SQQQ", "SPXL", "SPXS", "SOXL", "SOXS", "UVXY", "VXX", "SVXY",
    "TLT", "HYG", "LQD", "EEM", "EFA", "FXI", "EWZ", "USO", "UNG", "GLD",
    "SLV", "BITO", "IBIT", "FBTC",
    # single-stock leveraged/inverse wrappers (name filter is primary; these
    # are belt-and-braces for the most screener-prone ones)
    "PTIR", "PLTU", "PLTD", "TSLL", "TSLQ", "TSLT", "TSLZ", "NVDL", "NVDU",
    "NVDD", "NVDX", "NVDQ", "CONL", "CONI", "MSTU", "MSTX", "MSTZ", "SMCX",
    "SMCL", "AMDL", "AMDS", "AAPU", "AAPD", "GGLL", "GGLS", "METU", "METD",
    "AMZU", "AMZD", "MSFU", "MSFD", "AVL", "AVS", "PALU", "HOOX", "COIX",
}

# Name-based ETF/ETN detection (v0.12.14) — the primary exclusion layer.
# Word-bounded and issuer-anchored to avoid false positives on operating
# companies ("Build-A-Bear Workshop" must NOT match on "Bear").
_ETF_NAME_WORDS = ("ETF", "ETN")
_ETF_ISSUER_PREFIXES = (
    "PROSHARES", "DIREXION", "GRANITESHARES", "DEFIANCE", "YIELDMAX",
    "TRADR", "T-REX", "LEVERAGE SHARES", "VOLATILITY SHARES", "ROUNDHILL",
    "GLOBAL X", "ISHARES", "SPDR", "VANGUARD", "INVESCO", "WISDOMTREE",
    "FIRST TRUST", "VANECK", "SIMPLIFY", "AXS ",
)


# Non-common-stock instrument shapes (v0.12.28). The spec has always said
# "listed exchange, common stock/ADR — no OTC, no warrants/units", but nothing
# enforced it before the expensive per-ticker measurement. On 2026-08-13 the
# scanner spent its entire session on these: BBBY.WS, PSQH.WS, DAVEW, EDBLW,
# HOLOW, KWMWW, MRNOW, ASTLW, DFDVW, BBLGW, PECEW, AHT.PRD/PRG/PRH/PRI. Every
# one is a guaranteed PRICE_FLOOR or DOLLAR_VOLUME reject, and each one cost
# four market-data calls to reach that conclusion.
#
# Deliberately narrow: the dotted CQS/CMS suffixes, plus the Nasdaq
# fifth-letter convention where a FIVE-character root ending W/R/U means
# warrant / right / unit. Four-letter tickers are never matched — this is a
# cheap first pass, not the whole filter, and a false positive costs a real
# trade while a false negative costs only the measurement we already pay.
_DERIVATIVE_SUFFIX_RE = (
    r"(?:"
    r"\.(?:WS|WSA|WSB|U|UN|RT|RTS|PR[A-Z]?)$"   # BBBY.WS, XYZ.U, AHT.PRD
    r"|^[A-Z]{4}[WRU]$"                          # DAVEW, EDBLW, KWMWW, HOLOW
    r")"
)


def looks_like_derivative(symbol: Optional[str]) -> bool:
    """True for warrants, rights, units and preferred shares — instruments the
    scanner spec excludes and that can never clear min_price/ADV anyway.

    Pure, and conservative by construction: it matches SUFFIX SHAPE only, so
    an operating company with a normal 1-4 letter ticker is never caught. The
    point is to spend the scan budget on names that could actually trade, not
    to be exhaustive."""
    if not symbol:
        return False
    return bool(re.search(_DERIVATIVE_SUFFIX_RE, symbol.upper()))


def looks_like_etf(asset_name: Optional[str]) -> bool:
    """True when the asset's official name identifies an ETF/ETN/leveraged
    wrapper. Pure and conservative: word-bounded 'ETF'/'ETN', a leverage
    multiplier token ('2X', '1.5X', '-1X'), a fund word, or a known fund
    issuer prefix. None/empty name -> False (the ticker sets still apply)."""
    if not asset_name:
        return False
    up = asset_name.upper()
    if any(re.search(rf"\b{w}\b", up) for w in _ETF_NAME_WORDS):
        return True
    if re.search(r"(?<![A-Z0-9])-?\d(?:\.\d)?X\b", up):     # 2X / 1.5X / -1X
        return True
    if re.search(r"\bFUND\b", up):
        return True
    return any(up.startswith(p) for p in _ETF_ISSUER_PREFIXES)


@dataclass
class CandidateMetrics:
    """Everything measured about a candidate — journaled verbatim either way."""
    ticker: str
    price: float
    prev_close: Optional[float]
    move_pct: Optional[float]           # vs prev close (0.062 = +6.2%; v0.13:
                                        # NEGATIVE for a down-mover)
    adv20_dollars: Optional[float]
    rel_volume: Optional[float]         # day pace vs ADV(20)
    minutes_since_hod: Optional[int]    # freshness vs the day's HIGH (up-movers)
    spread_bps: Optional[float]
    luld_headroom_pct: Optional[float]  # approx: distance to the RELEVANT
                                        # 10% band from the 5-min ref (v0.13:
                                        # up-band for gainers, down-band for
                                        # losers)
    vwap: Optional[float]
    day_high: Optional[float]
    detected_ts: str = ""
    minutes_since_lod: Optional[int] = None  # v0.13: freshness vs the day's
    day_low: Optional[float] = None          # LOW (down-movers)

    @property
    def is_down(self) -> bool:
        """v0.13: a down-mover (loser leg) — mirrored checks apply."""
        return (self.move_pct or 0.0) < 0

    @property
    def move_magnitude(self) -> Optional[float]:
        return None if self.move_pct is None else abs(self.move_pct)

    @property
    def minutes_since_extreme(self) -> Optional[int]:
        """Freshness vs the extreme that matters for this direction."""
        return self.minutes_since_lod if self.is_down else self.minutes_since_hod

    def payload(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def luld_headroom(last: float, ref_price_5m: Optional[float],
                  direction: str = "up") -> Optional[float]:
    """Approximate LULD band headroom in the direction the move is going.
    Tier-1 RTH band is ±5% and Tier-2 ±10% of the 5-min reference price;
    without a security-tier feed we use the WIDER 10% band and let
    min_luld_headroom_pct provide the margin. v0.13: a down-mover's relevant
    band is the LOWER one. Honest approximation, journaled as such."""
    if not ref_price_5m or not last or last <= 0:
        return None
    if direction == "down":
        band_down = ref_price_5m * 0.90
        return round(max(last - band_down, 0.0) / last, 4)
    band_up = ref_price_5m * 1.10
    return round(max(band_up - last, 0.0) / last, 4)


def filter_candidate(m: CandidateMetrics, cfg: dict,
                     is_etf: Optional[bool] = None,
                     earnings_next_sessions: Optional[int] = None,
                     asset_name: Optional[str] = None
                     ) -> Optional[str]:
    """First failing filter's reject code, or None = candidate passes.
    Null-safe: a metric that could not be computed fails CLOSED (the scanner
    proposes trades — missing evidence means no proposal; contrast with the
    TA context pack where null just means 'unavailable').

    v0.12.14 ETF exclusion layers, any hit -> ETF_EXCLUDED: explicit is_etf
    flag, the KNOWN_ETFS ticker set, the operator's cfg etf_denylist, or the
    asset's official name (looks_like_etf)."""
    if cfg.get("exclude_etfs", True):
        denylist = {str(t).upper() for t in (cfg.get("etf_denylist") or [])}
        etf = (is_etf if is_etf is not None
               else (m.ticker in KNOWN_ETFS or m.ticker in denylist
                     or looks_like_etf(asset_name)))
        if etf:
            return "ETF_EXCLUDED"
    if m.price is None or m.price < float(cfg["min_price"]):
        return "PRICE_FLOOR"
    if not m.adv20_dollars or m.adv20_dollars < float(cfg["min_adv20_dollars"]):
        return "DOLLAR_VOLUME"
    # v0.13: down-movers are candidates only when the loser leg is on; the
    # move requirement applies to MAGNITUDE, same bar both directions.
    if m.is_down and not cfg.get("include_losers", False):
        return "MOVE_PCT"
    if m.move_magnitude is None or m.move_magnitude < float(cfg["min_move_pct"]):
        return "MOVE_PCT"
    if m.rel_volume is None:
        return "NO_TAPE"
    if m.rel_volume < float(cfg["min_rel_volume"]):
        return "REL_VOLUME"
    if m.minutes_since_extreme is None \
            or m.minutes_since_extreme > int(cfg["max_minutes_since_hod"]):
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
    move = min(m.move_magnitude or 0.0, 0.15) / 0.15     # 0..1 (v0.13: abs)
    fresh = 1.0 - min(m.minutes_since_extreme or 60, 60) / 60.0
    spread = 1.0 - min(m.spread_bps or 40.0, 40.0) / 40.0
    return round(0.45 * rel + 0.30 * move + 0.15 * fresh + 0.10 * spread, 4)


def scan_mode(emitted_today: int, cfg: dict) -> str:
    """v0.13.8 — what a scan cycle may do once the daily cap is spent.

    'EMIT'    — budget remains; normal scanning.
    'OBSERVE' — daily cap reached: keep scanning and journaling (the doctrine
                is 'everything C10 SEES is journaled'), emit nothing.
    'IDLE'    — cap reached and observe_after_cap disabled (pre-v0.13.8
                behaviour, kept reachable by config).

    Incident 2026-08-19: all 6 daily emissions were spent 09:51:36–09:54:23
    and scan_once then returned early on every cycle — the scanner was blind
    from 09:54 to the 15:15 window close. MSTR ground +12.4% through the
    afternoon and has NO scanner_candidates row of any status: not seen, not
    measured, invisible. The cap was designed to limit trading, not
    observation."""
    if emitted_today >= int(cfg["max_per_day"]):
        return "OBSERVE" if cfg.get("observe_after_cap", True) else "IDLE"
    return "EMIT"


def emission_disposition(score: float, m: CandidateMetrics, cfg: dict, *,
                         emitted_this_scan: int, emitted_today: int,
                         emitted_last_hour: int, open_scanner: int,
                         etb_ok: Optional[bool] = None
                         ) -> Optional[tuple[str, str]]:
    """v0.13.8 — one pure decision for a ranked survivor: None = EMIT, else
    the (status, reject_reason) pair to journal. Check order is the point:

    1. SCORE_FLOOR   — a weak candidate must never consume budget. On
                       2026-08-19 half the day's 6 slots went to candidates
                       scoring 0.48–0.55 in the first three scans.
    2. SSR / borrow  — a down-mover whose short entry is already impossible
                       must never consume budget either. Both are knowable
                       BEFORE spending the slot: SSR is pure arithmetic
                       (Reg SHO trips at −10% from prior close, and C3's
                       ssr_veto fails closed on it), borrow is one cached
                       assets-API call. 2026-08-19: WYFI (no borrow) and
                       AXTI (through −10% by gate time) burned 2 of 6 slots
                       to reach guaranteed vetoes. NOTE the honest cost: a
                       filtered down-mover also loses its long-side
                       capitulation path — accepted, rare, and the row is
                       journaled for A9 to prove otherwise. etb_ok None
                       means 'not looked up' (precheck off / up-mover /
                       caller defers the API call): no borrow verdict here.
    3. the caps      — per-scan, per-day, per-hour (v0.13.8, pacing), then
                       concurrent positions. CAPPED rows, budget intact.
    """
    if score < float(cfg.get("min_emit_score", 0.0)):
        return ("FILTERED", "SCORE_FLOOR")
    if m.is_down and cfg.get("precheck_short_availability", True):
        if m.move_pct is not None \
                and m.move_pct <= float(cfg.get("ssr_trigger_pct", -0.10)):
            return ("FILTERED", "SSR_RESTRICTED")
        if etb_ok is False:
            return ("FILTERED", "SHORT_UNAVAILABLE")
    if emitted_this_scan >= int(cfg["max_per_scan"]):
        return ("CAPPED", "PER_SCAN")
    if emitted_today >= int(cfg["max_per_day"]):
        return ("CAPPED", "PER_DAY")
    hour_cap = int(cfg.get("max_per_hour", 0))
    if hour_cap and emitted_last_hour >= hour_cap:
        return ("CAPPED", "PER_HOUR")
    if open_scanner >= int(cfg["max_concurrent_positions"]):
        return ("CAPPED", "CONCURRENT")
    return None


def scanner_headline(m: CandidateMetrics, news_match: str) -> str:
    """The synthetic item's headline — honest about what this signal is."""
    tag = {"none": "no news match",
           "weak": "peer/sector headlines only",
           "strong": "news match"}.get(news_match, news_match)
    return (f"SCANNER: {m.ticker} {(m.move_pct or 0) * 100:+.1f}% on "
            f"{m.rel_volume:.1f}x relative volume — {tag}")


def in_scan_window(now_et_hhmm: str, cfg: dict) -> bool:
    return cfg["session_start_et"] <= now_et_hhmm < cfg["session_end_et"]
