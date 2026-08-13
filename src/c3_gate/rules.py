"""C3 Market Confirmation Gate rules (code — the primary anti-overtrading
control). Pure functions over a MarketState snapshot; the service does I/O.

Check order (cheapest first, all journaled on veto):
  1. LONG_ONLY        direction != "up" -> no entry path exists (long-only book)
  2. CREDIBILITY      corroboration matrix: required independent outlets =
                      f(impact bucket, source tier); Tier-1 passes alone;
                      source_risk="high" raises the requirement one level
                      (v0.12.28: EFFECTIVE outlets = independent outlets +
                      capped cluster-growth credit. We subscribe to exactly
                      one journalistic wire, so independent_outlets can never
                      exceed 1 on a reported story and the high-impact
                      requirement of 2 was unsatisfiable by arithmetic —
                      WDAY/Silver Lake, 2026-08-13. See growth_credit().)
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
                STALE_MARKETDATA (v0.12.28) every other check passed but the
                newest bar backing the decision is older than
                max_bar_age_secs. Re-checkable: a cache heals, and stale data
                must never be the thing that MANUFACTURES an entry.
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
    cluster_items: int = 1             # v0.12.28: distinct items on the
                                       # cluster (by item_id; revisions of the
                                       # same item are NOT growth)
    bar_age_secs: Optional[float] = None  # v0.12.28: age of the newest minute
                                       # bar backing this evaluation. None =
                                       # unknown (staleness check skipped)


@dataclass
class GateVerdict:
    verdict: str                       # PASS | VETO
    rule: str                          # intraday | open_handoff
    veto_reason: Optional[str] = None
    numbers: dict | None = None        # journaled either way


def _bar_stale(bar_age_secs: Optional[float], cfg: dict) -> bool:
    """v0.12.28: is the market data behind this evaluation too old to justify
    an entry? Unknown age (None) is NOT stale — the check is additive and must
    not veto callers that don't supply the field."""
    max_age = float(cfg.get("max_bar_age_secs", 0) or 0)
    if max_age <= 0 or bar_age_secs is None:
        return False
    return float(bar_age_secs) > max_age


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


def growth_credit(independent_outlets: int, cluster_items: int,
                  cfg: dict) -> int:
    """v0.12.28: effective-outlet credit earned by a story cluster GROWING.

    A cluster member is, by C2's construction, an article similar enough to be
    the same story and different enough to survive the >=0.9 cosine dedup — a
    substantively distinct write-up, not a repost. Follow-on articles are
    therefore evidence the story is real, even when they all come from the one
    wire we subscribe to. They are worth less than an independent outlet, so
    the credit is capped (default +1) and can never carry a lone item.

    Pure. Returns 0 when disabled, when the cluster has not grown, or when
    inputs are degenerate."""
    gcfg = cfg.get("cluster_growth") or {}
    if not gcfg.get("enabled", False):
        return 0
    per = int(gcfg.get("items_per_credit", 1))
    cap = int(gcfg.get("max_credit", 1))
    if per <= 0 or cap <= 0:
        return 0
    # growth = distinct items beyond the outlets already counted independently
    extra = max(int(cluster_items) - max(int(independent_outlets), 1), 0)
    return min(extra // per, cap)


def evaluate(thesis: dict, state: MarketState, cfg: dict) -> GateVerdict:
    pct_move = ((state.last_price - state.prenews_price) / state.prenews_price
                if state.prenews_price else 0.0)
    credit = growth_credit(state.corroboration_outlets, state.cluster_items,
                           cfg)
    effective_outlets = state.corroboration_outlets + credit
    numbers = {"pct_move": round(pct_move, 5), "vol_mult": state.vol_mult,
               "minutes": state.minutes_since_publish,
               "gap_pct": state.gap_pct,
               "bar_age_secs": state.bar_age_secs,
               "corroboration": {"independent_outlets": state.corroboration_outlets,
                                 "cluster_items": state.cluster_items,
                                 "growth_credit": credit,
                                 "effective_outlets": effective_outlets,
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
    if effective_outlets < required:
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
        # v0.12.28: last gate before a PASS — an entry may not be built on a
        # stale bar. Re-checkable, so a transient cache heals instead of
        # killing the signal.
        if _bar_stale(state.bar_age_secs, cfg):
            return GateVerdict("VETO", rule, "STALE_MARKETDATA", numbers)
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
# Extended-hours SHADOW branch (v0.12.11) — observe-only
# ---------------------------------------------------------------------------

@dataclass
class EHState:
    """Quote-based state for the extended-hours shadow evaluation. There is
    no vol_mult / VWAP here on purpose: pre/post books are thin and the RTH
    volume machinery is meaningless — liquidity is judged from the live
    quote (two-sided market + spread), which is also exactly what a real EH
    limit order would face."""
    prenews_price: float
    last_price: float
    bid: Optional[float]
    ask: Optional[float]
    spread_bps: Optional[float]
    minutes_since_publish: int
    session: str                       # 'pre' | 'post'
    corroboration_outlets: int
    tier_min: int
    cluster_items: int = 1             # v0.12.28 (same growth credit as RTH)


def evaluate_eh(thesis: dict, s: EHState, cfg: dict) -> GateVerdict:
    """Would we have traded this in extended hours? Verdict WOULD_TRADE is
    journaled and measured (counterfactuals) but NEVER executed — no
    signal.risk message exists on this branch, by construction.

    Check order mirrors the intraday rule where semantics carry over
    (LONG_ONLY, CREDIBILITY, freshness window, already-extended) and
    replaces volume confirmation with quote-based liquidity: a real
    two-sided market tighter than eh_shadow.max_spread_bps. One-shot by
    design — the strategy being measured is 'trade the news AT ARRIVAL in
    the extended session'; there is no re-check window here."""
    ecfg = cfg.get("eh_shadow") or {}
    pct_move = ((s.last_price - s.prenews_price) / s.prenews_price
                if s.prenews_price else 0.0)
    credit = growth_credit(s.corroboration_outlets, s.cluster_items, cfg)
    effective_outlets = s.corroboration_outlets + credit
    numbers = {"pct_move": round(pct_move, 5), "bid": s.bid, "ask": s.ask,
               "spread_bps": s.spread_bps,
               "minutes": s.minutes_since_publish, "session": s.session,
               "corroboration": {"independent_outlets": s.corroboration_outlets,
                                 "cluster_items": s.cluster_items,
                                 "growth_credit": credit,
                                 "effective_outlets": effective_outlets,
                                 "tier_min": s.tier_min}}
    rule = "eh_shadow"

    if thesis["direction"] != "up":
        return GateVerdict("VETO", rule, "LONG_ONLY", numbers)

    impact = _impact_bucket(float(thesis["magnitude_est"]), cfg)
    required = credibility_required(impact, s.tier_min,
                                    thesis["source_risk"], cfg)
    numbers["credibility"] = {"impact": impact, "required_outlets": required}
    if effective_outlets < required:
        return GateVerdict("VETO", rule, "CREDIBILITY", numbers)

    if s.minutes_since_publish > float(ecfg.get("window_min", 30)):
        return GateVerdict("VETO", rule, "GATE_WINDOW", numbers)
    if pct_move >= cfg["extended_pct"]:
        return GateVerdict("VETO", rule, "GATE_EXTENDED", numbers)

    # liquidity: a real, two-sided, tradeable quote — the EH equivalent of
    # confirmation. Missing/one-sided/inverted quotes and wide spreads all
    # fail here (fail closed: this branch PROPOSES a hypothetical entry).
    if (s.bid is None or s.ask is None or s.bid <= 0 or s.ask <= s.bid
            or s.last_price <= 0 or s.spread_bps is None):
        return GateVerdict("VETO", rule, "EH_LIQUIDITY", numbers)
    if s.spread_bps > float(ecfg.get("max_spread_bps", 100)):
        return GateVerdict("VETO", rule, "EH_LIQUIDITY", numbers)

    numbers["hypothetical_entry"] = s.ask     # a marketable EH limit buys the ask
    return GateVerdict("WOULD_TRADE", rule, None, numbers)


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

