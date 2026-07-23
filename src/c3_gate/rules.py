"""C3 Market Confirmation Gate rules (code — the primary anti-overtrading
control). Pure functions over a MarketState snapshot; the service does I/O.

Check order (cheapest first, all journaled on veto):
  1. LONG_ONLY        direction != "up" -> no entry path exists (long-only book)
  2. CREDIBILITY      corroboration matrix: required independent outlets =
                      f(impact bucket, source tier); Tier-1 passes alone;
                      source_risk="high" raises the requirement one level
  3. intraday vs open-handoff branch on whether the news arrived in-session:
     intraday:  GATE_WINDOW    minutes_since_publish > N
                GATE_EXTENDED  already >= extended_pct from pre-news
                MARKETDATA_MISSING (v0.5.9) vol_mult is None — no volume bars
                came back, so the gate CANNOT evaluate confirmation. Still a
                veto (fail safe), but journaled distinctly so a starved data
                feed can never masquerade as "the market didn't confirm".
                (v0.11.10: the service now DEFERS evaluation until the
                since-window can contain min_confirm_bars completed minute
                bars, so this veto only fires when a MATURE window is empty —
                a trading halt or a genuine data outage, not a fast signal.)
                GATE_NO_CONFIRM pct_move < X or vol_mult < Y
     handoff:   GATE_OPEN_WINDOW first 15 minutes after open
                PRICED_IN      gap >= gap_ratio * magnitude_est

All thresholds from config/gate.yaml — PLACEHOLDER values pending the §14
gate-threshold design item; the rule SHAPES are per baseline v0.5.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class MarketState:
    """Everything the rules need, computed by the service from market data."""
    prenews_price: float
    last_price: float
    vol_mult: Optional[float]          # since-news minute volume / baseline
    minutes_since_publish: int
    news_in_session: bool              # published during RTH -> intraday rule
    minutes_since_open: Optional[int]  # None when market closed
    gap_pct: Optional[float]           # today's open vs prev close (handoff)
    corroboration_outlets: int
    tier_min: int                      # best (lowest) tier in the cluster


@dataclass
class GateVerdict:
    verdict: str                       # PASS | VETO
    rule: str                          # intraday | open_handoff
    veto_reason: Optional[str] = None
    numbers: dict | None = None        # journaled either way


def _impact_bucket(magnitude_est: float, cfg: dict) -> str:
    if magnitude_est >= cfg["impact_high_min"]:
        return "high"
    if magnitude_est >= cfg["impact_medium_min"]:
        return "medium"
    return "low"


def credibility_required(impact: str, tier_min: int, source_risk: str,
                         cfg: dict) -> int:
    """Required independent outlets. Tier-1 filing passes alone (returns 1).
    High source_risk bumps the impact bucket one level."""
    if tier_min == 1:
        return 1
    order = ["low", "medium", "high"]
    if source_risk == "high":
        impact = order[min(order.index(impact) + 1, 2)]
    return int(cfg["required_outlets"][impact][tier_min])


def evaluate(thesis: dict, state: MarketState, cfg: dict) -> GateVerdict:
    pct_move = ((state.last_price - state.prenews_price) / state.prenews_price
                if state.prenews_price else 0.0)
    numbers = {"pct_move": round(pct_move, 5), "vol_mult": state.vol_mult,
               "minutes": state.minutes_since_publish,
               "gap_pct": state.gap_pct,
               "corroboration": {"independent_outlets": state.corroboration_outlets,
                                 "tier_min": state.tier_min}}
    rule = "intraday" if state.news_in_session else "open_handoff"

    # 1. long-only
    if thesis["direction"] != "up":
        return GateVerdict("VETO", rule, "LONG_ONLY", numbers)

    # 2. credibility
    impact = _impact_bucket(float(thesis["magnitude_est"]), cfg)
    required = credibility_required(impact, state.tier_min,
                                    thesis["source_risk"], cfg)
    numbers["credibility"] = {"impact": impact, "required_outlets": required}
    if state.corroboration_outlets < required:
        return GateVerdict("VETO", rule, "CREDIBILITY", numbers)

    # 3a. intraday confirmation
    if rule == "intraday":
        if state.minutes_since_publish > cfg["intraday_window_min"]:
            return GateVerdict("VETO", rule, "GATE_WINDOW", numbers)
        if pct_move >= cfg["extended_pct"]:
            return GateVerdict("VETO", rule, "GATE_EXTENDED", numbers)
        if state.vol_mult is None:
            # v0.5.9: no volume data is NOT the same as no confirmation.
            return GateVerdict("VETO", rule, "MARKETDATA_MISSING", numbers)
        if pct_move < cfg["intraday_move_pct"] \
                or state.vol_mult < cfg["intraday_vol_mult"]:
            return GateVerdict("VETO", rule, "GATE_NO_CONFIRM", numbers)
        return GateVerdict("PASS", rule, None, numbers)

    # 3b. open handoff
    if state.minutes_since_open is None or state.minutes_since_open < cfg["open_blackout_min"]:
        return GateVerdict("VETO", rule, "GATE_OPEN_WINDOW", numbers)
    if state.gap_pct is not None and \
            state.gap_pct >= cfg["handoff_gap_ratio"] * float(thesis["magnitude_est"]):
        return GateVerdict("VETO", rule, "PRICED_IN", numbers)
    # small gap on rated news = the opportunity; still demand some confirmation
    if pct_move >= cfg["extended_pct"]:
        return GateVerdict("VETO", rule, "GATE_EXTENDED", numbers)
    return GateVerdict("PASS", rule, None, numbers)


# ---------------------------------------------------------------------------
# Scanner-origin branch (v0.12.1)
# ---------------------------------------------------------------------------

@dataclass
class ScannerState:
    """Everything the scanner rules need — computed by the service NOW, at
    gate time; detection-time numbers are re-checked, never trusted."""
    last_price: float
    detect_price: float                # C10's snapshot price
    minutes_since_detect: float
    vwap: Optional[float]              # today's session VWAP
    range30_pos: Optional[float]       # position in last-30-min range (0..1)
    bar5_range_ratio: Optional[float]  # last completed 5-min bar range / avg
    spread_bps: Optional[float]
    halted: bool = False


def evaluate_scanner(thesis: dict, s: ScannerState, cfg: dict) -> GateVerdict:
    """The scanner branch asks "is this still tradeable", not "did the move
    happen" — a scanner signal is BORN confirmed (the move IS the signal), so
    confirmation, credibility and the extended-skip do not apply. LONG_ONLY
    survives unchanged. Every check re-measures the tape at gate time.

    Null policy: this branch PROPOSES an entry, so missing evidence fails
    CLOSED (contrast v0.11.10's defer, which protects news signals from
    missing TIME — here the bars exist or the setup is wrong)."""
    run_since_detect = ((s.last_price - s.detect_price) / s.detect_price
                        if s.detect_price else 0.0)
    numbers = {"last": s.last_price, "detect_price": s.detect_price,
               "run_since_detect_pct": round(run_since_detect, 5),
               "minutes_since_detect": round(s.minutes_since_detect, 1),
               "vwap": s.vwap, "range30_pos": s.range30_pos,
               "bar5_range_ratio": s.bar5_range_ratio,
               "spread_bps": s.spread_bps, "halted": s.halted}
    rule = "scanner"

    if thesis["direction"] != "up":
        return GateVerdict("VETO", rule, "LONG_ONLY", numbers)

    # 1. staleness — chasing is how mean reversion collects its fee
    if s.minutes_since_detect > float(cfg["stale_max_min"]) \
            or run_since_detect > float(cfg["stale_run_pct"]):
        return GateVerdict("VETO", rule, "SCANNER_STALE", numbers)

    # 2. structure — below VWAP or lower half of the recent range is
    #    distribution, not continuation
    if cfg.get("require_above_vwap", True):
        if s.vwap is None or s.last_price < s.vwap:
            return GateVerdict("VETO", rule, "SCANNER_STRUCTURE", numbers)
    if s.range30_pos is None or s.range30_pos < float(cfg["range30_min_pos"]):
        return GateVerdict("VETO", rule, "SCANNER_STRUCTURE", numbers)

    # 3. parabolic — a vertical bar is the exhaustion print, not the entry
    if s.bar5_range_ratio is None \
            or s.bar5_range_ratio > float(cfg["parabolic_bar_ratio"]):
        return GateVerdict("VETO", rule, "SCANNER_PARABOLIC", numbers)

    # 4. liquidity — re-checked NOW (halts + spreads change in minutes)
    if s.halted:
        return GateVerdict("VETO", rule, "SCANNER_LIQUIDITY", numbers)
    if s.spread_bps is None or s.spread_bps > float(cfg["max_spread_bps"]):
        return GateVerdict("VETO", rule, "SCANNER_LIQUIDITY", numbers)

    return GateVerdict("PASS", rule, None, numbers)

