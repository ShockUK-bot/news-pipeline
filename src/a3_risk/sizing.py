"""A3 sizing chain (phase4-design-v1_0 §2) — pure functions, no I/O.

Models propose, code disposes: the LLM's only inputs here are k /
realization_fraction / time_window (already band-validated); every dollar
figure below is deterministic code. Allocation is a consequence of risk:
size = risk_budget / stop_distance, then clipped, then viability-checked.

Veto reasons (all journaled with numbers): KILL_SWITCH, BREAKER,
BLOCK_ENTRIES, HALTED, MAX_TRADES, ENTRY_BLACKOUT, WIDE_SPREAD,
EARNINGS_BLACKOUT, NO_ATR, SIZE_CLIPPED.
Flags (journaled, non-blocking): EARNINGS_UNKNOWN, SECTOR_UNKNOWN (D7).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class SizingInputs:
    # capital (from C4's reconciliation rows — A3 never calls the broker)
    effective_capital: float           # min(broker_equity, trading_capital)
    settled_cash: float
    # gate snapshot (C3)
    ref_price: float                   # snapshot ask-side reference
    bid: float
    ask: float
    spread_bps: float
    atr_14: Optional[float]
    adv_20d: Optional[float]
    # portfolio state
    open_heat: dict                    # {"SHORT": $risk, "LONG": $risk}
    deployed_notional: float
    trades_today: int
    ticker_halted: bool = False
    # operational controls / flags
    kill_switch: bool = False
    breaker: bool = False
    block_entries: bool = False
    max_trades_per_day: int = 5
    # session
    minutes_to_close: Optional[int] = None    # None off-hours (entries blocked upstream)
    # deferred-nullable context (D7)
    earnings_next_sessions: Optional[int] = None   # sessions until earnings; None unknown
    sector: Optional[str] = None
    sector_heat: Optional[float] = None


@dataclass
class SizingResult:
    verdict: str                       # SIZE | VETO
    veto_reason: Optional[str] = None
    qty: int = 0
    limit_price: float = 0.0
    stop_distance: float = 0.0
    initial_stop: float = 0.0
    catastrophe_stop: float = 0.0
    risk_budget: float = 0.0           # intended $ risk
    actual_risk: float = 0.0           # qty * stop_distance after clips
    numbers: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


def limit_price_from_snapshot(ask: float, spread_bps: float) -> float:
    """Snapshot ask + min(half-spread, 10bps) buffer — priced off the C3
    snapshot per baseline; buffer bounds chase without paying full spread."""
    buffer = min((spread_bps / 2) / 10_000, 0.0010) * ask
    return round(ask + buffer, 2)


def hard_gates(inp: SizingInputs, limits_cfg: dict, profile: dict,
               earnings_blackout_sessions: int = 1
               ) -> tuple[Optional[SizingResult], dict, list[str]]:
    """Absolute vetoes, cheapest first — separable so A3 can run them BEFORE
    the discretion model call (no LLM tokens burned under a kill switch).
    Returns (veto_result | None, numbers_so_far, flags_so_far)."""
    n: dict = {}
    flags: list[str] = []
    if inp.kill_switch:
        return SizingResult("VETO", "KILL_SWITCH", numbers=n), n, flags
    if inp.breaker:
        return SizingResult("VETO", "BREAKER", numbers=n), n, flags
    if inp.block_entries:
        return SizingResult("VETO", "BLOCK_ENTRIES", numbers=n), n, flags
    if inp.ticker_halted:
        return SizingResult("VETO", "HALTED", numbers=n), n, flags
    n["trades_today"] = inp.trades_today
    if inp.trades_today >= inp.max_trades_per_day:
        n["max_trades_per_day"] = inp.max_trades_per_day
        return SizingResult("VETO", "MAX_TRADES", numbers=n), n, flags
    if inp.minutes_to_close is not None and \
            inp.minutes_to_close <= limits_cfg["entry_blackout_final_min"]:
        n["minutes_to_close"] = inp.minutes_to_close
        return SizingResult("VETO", "ENTRY_BLACKOUT", numbers=n), n, flags
    n["spread_bps"] = inp.spread_bps
    if inp.spread_bps > limits_cfg["spread_max_bps"]:
        return SizingResult("VETO", "WIDE_SPREAD", numbers=n), n, flags
    if inp.earnings_next_sessions is None:
        flags.append("EARNINGS_UNKNOWN")           # D7: allow + flag during paper
    elif profile.get("earnings_blackout_exit") and \
            inp.earnings_next_sessions <= earnings_blackout_sessions:
        n["earnings_next_sessions"] = inp.earnings_next_sessions
        return SizingResult("VETO", "EARNINGS_BLACKOUT", numbers=n,
                            flags=flags), n, flags
    if inp.atr_14 is None or inp.atr_14 <= 0:
        return SizingResult("VETO", "NO_ATR", numbers=n, flags=flags), n, flags
    return None, n, flags


def size_entry(inp: SizingInputs, capital_cfg: dict, limits_cfg: dict,
               profile: dict, horizon: str, k_adj: float,
               earnings_blackout_sessions: int = 1) -> SizingResult:
    veto, n, flags = hard_gates(inp, limits_cfg, profile,
                                earnings_blackout_sessions)
    if veto is not None:
        return veto

    # ---- the chain -------------------------------------------------------------
    risk_budget = capital_cfg["risk_per_trade_pct"] * inp.effective_capital
    stop_distance = k_adj * inp.atr_14
    limit_price = limit_price_from_snapshot(inp.ask, inp.spread_bps)
    raw_qty = risk_budget / stop_distance
    n.update(risk_budget=round(risk_budget, 2),
             stop_distance=round(stop_distance, 4),
             limit_price=limit_price, k_adj=k_adj, raw_qty=round(raw_qty, 2))

    clips: dict[str, float] = {}
    # notional cap
    clips["notional"] = (capital_cfg["max_position_notional_pct"]
                         * inp.effective_capital) / limit_price
    # liquidity cap
    if inp.adv_20d:
        clips["adv"] = limits_cfg["adv_participation_max"] * inp.adv_20d
    # settled buying power (cash account)
    clips["settled_cash"] = max(inp.settled_cash, 0.0) / limit_price
    # deployed-notional pre-flight headroom
    clips["capital_headroom"] = max(
        inp.effective_capital - inp.deployed_notional, 0.0) / limit_price
    # portfolio heat, per-lane split
    lane_cap = capital_cfg["heat_split"][horizon] * inp.effective_capital
    lane_used = inp.open_heat.get(horizon, 0.0)
    clips["lane_heat"] = max(lane_cap - lane_used, 0.0) / stop_distance
    total_cap = capital_cfg["max_portfolio_heat_pct"] * inp.effective_capital
    total_used = sum(inp.open_heat.values())
    clips["total_heat"] = max(total_cap - total_used, 0.0) / stop_distance
    # sector heat (deferred-nullable, D7)
    if inp.sector is None:
        flags.append("SECTOR_UNKNOWN")
    elif inp.sector_heat is not None:
        sector_cap = capital_cfg["max_sector_heat_pct"] * inp.effective_capital
        clips["sector_heat"] = max(sector_cap - inp.sector_heat, 0.0) / stop_distance

    qty = math.floor(min(raw_qty, *clips.values()))
    binding = min(clips, key=lambda k_: clips[k_])
    n["clips"] = {k_: round(v, 2) for k_, v in clips.items()}
    n["binding_clip"] = binding if clips[binding] < raw_qty else None
    n["qty"] = qty

    actual_risk = qty * stop_distance
    n["actual_risk"] = round(actual_risk, 2)
    if qty <= 0 or actual_risk < capital_cfg["min_viable_risk_fraction"] * risk_budget:
        n["min_viable_risk_fraction"] = capital_cfg["min_viable_risk_fraction"]
        return SizingResult("VETO", "SIZE_CLIPPED", numbers=n, flags=flags)

    initial_stop = round(limit_price - stop_distance, 2)
    cat_k = profile["catastrophe"]["k"]
    catastrophe_stop = round(limit_price - cat_k * inp.atr_14, 2)
    n["catastrophe_k"] = cat_k

    return SizingResult("SIZE", None, qty=qty, limit_price=limit_price,
                        stop_distance=round(stop_distance, 4),
                        initial_stop=initial_stop,
                        catastrophe_stop=catastrophe_stop,
                        risk_budget=round(risk_budget, 2),
                        actual_risk=round(actual_risk, 2),
                        numbers=n, flags=flags)


def open_risk_dollars(qty_open: int, avg_entry: float, current_stop: float) -> float:
    """A position's contribution to portfolio heat: current stop distance x
    open shares (stop at/above entry contributes zero — house money)."""
    return max(avg_entry - current_stop, 0.0) * qty_open

