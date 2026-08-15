"""Directional arithmetic in ONE place (v0.13, short selling).

Every formula in the system that cares whether a position is long or short
routes through these helpers. Nothing else in the codebase may write
`if side == "SHORT"` around arithmetic — that is how sign bugs breed.

Vocabulary (deliberately distinct from `horizon`, whose values are also
"SHORT"/"LONG" but mean *time*, not direction):

  position side : "LONG" | "SHORT"        (positions.side column)
  intent side   : "BUY"  | "SELL"         longs  (entry / exit)
                  "SELL_SHORT" | "BUY_TO_COVER"  shorts (entry / exit)
  thesis        : direction "up" -> LONG book, "down" -> SHORT book

Invariants these helpers preserve:
  * r_unit = dir * (avg_entry - initial_stop)  is POSITIVE for both sides
    (schema CHECK positions.r_unit > 0 is kept).
  * "tighter" for a stop always means CLOSER to current price from the
    losing side: higher for longs, lower for shorts.
"""
from __future__ import annotations

from typing import Literal

Side = Literal["LONG", "SHORT"]

ENTRY_INTENT = {"LONG": "BUY", "SHORT": "SELL_SHORT"}
EXIT_INTENT = {"LONG": "SELL", "SHORT": "BUY_TO_COVER"}
ENTRY_INTENTS = frozenset(ENTRY_INTENT.values())   # {'BUY','SELL_SHORT'}
EXIT_INTENTS = frozenset(EXIT_INTENT.values())     # {'SELL','BUY_TO_COVER'}

# what the broker API actually accepts (Alpaca: buy/sell only; a sell from
# flat opens a short, a buy against a short covers)
BROKER_SIDE = {"BUY": "BUY", "SELL": "SELL",
               "SELL_SHORT": "SELL", "BUY_TO_COVER": "BUY"}


def side_for(thesis_direction: str) -> Side:
    """Thesis direction ("up"/"down") -> position side."""
    return "LONG" if thesis_direction == "up" else "SHORT"


def dir_mult(side: Side) -> int:
    """+1 for LONG, -1 for SHORT. The one sign in the system."""
    return 1 if side == "LONG" else -1


def entry_stop(side: Side, limit_price: float, distance: float) -> float:
    """Initial/catastrophe stop price: below entry for longs, above for shorts."""
    return round(limit_price - dir_mult(side) * distance, 2)


def r_unit(side: Side, avg_entry: float, initial_stop: float) -> float:
    """Risk unit; positive for both sides by construction."""
    return round(dir_mult(side) * (avg_entry - initial_stop), 4)


def pnl(side: Side, avg_entry: float, price: float, qty: int) -> float:
    """Unrealized/realized P&L for qty shares."""
    return round(dir_mult(side) * (price - avg_entry) * qty, 4)


def r_progress(side: Side, avg_entry: float, r_unit_: float, price: float) -> float:
    """Signed progress in R: positive = winning, for both sides."""
    return dir_mult(side) * (price - avg_entry) / r_unit_


def move_fraction(side: Side, avg_entry: float, mark: float) -> float:
    """Fraction of entry price moved in our favor (for magnitude realization)."""
    return dir_mult(side) * (mark - avg_entry) / avg_entry


def realization_target(side: Side, avg_entry: float, fraction: float,
                       magnitude: float) -> float:
    """Price at which `fraction` of the predicted magnitude is realized:
    above entry for longs, below for shorts."""
    return round(avg_entry * (1.0 + dir_mult(side) * fraction * magnitude), 4)


def target_hit(side: Side, bar: dict, target: float) -> bool:
    """Did this bar touch the realization target on the winning side?"""
    return bar["high"] >= target if side == "LONG" else bar["low"] <= target


def stop_hit(side: Side, bar: dict, stop: float) -> bool:
    """Did this bar touch the stop on the losing side?"""
    return bar["low"] <= stop if side == "LONG" else bar["high"] >= stop


def is_tighter(side: Side, proposed: float, current: float) -> bool:
    """True if `proposed` is a strictly tighter stop than `current`.
    Tighter = higher for longs, lower for shorts. (Rule 16: never loosen.)"""
    return proposed > current if side == "LONG" else proposed < current


def watermark(side: Side, current: float | None, bar: dict) -> float:
    """Best-excursion watermark: high-water for longs, LOW-water for shorts."""
    edge = bar["high"] if side == "LONG" else bar["low"]
    if current is None:
        return edge
    return max(current, edge) if side == "LONG" else min(current, edge)


def trail_from(side: Side, mark_water: float, distance: float) -> float:
    """Trailing stop from the watermark: below it for longs, above for shorts."""
    return round(mark_water + (-1 if side == "LONG" else 1) * distance, 2)


def marketable_exit(side: Side, ref_price: float, slip: float = 0.003) -> float:
    """Aggressive exit limit: under the mark to sell a long, OVER it to cover
    a short. `slip` = fraction of price conceded to get done."""
    mult = 1.0 - slip if side == "LONG" else 1.0 + slip
    return round(ref_price * mult, 2)


def open_risk(side: Side, avg_entry: float, current_stop: float,
              qty_open: int) -> float:
    """$ at risk to the current stop; zero once the stop is at/through entry.
    (Long-only version silently returned 0 for shorts — that would have let
    a short book defeat every portfolio heat cap.)"""
    return max(dir_mult(side) * (avg_entry - current_stop), 0.0) * qty_open
