"""D1 overnight-hold rule (phase4-design-v1_0).

Deterministic C4 rule at 15:45 ET for the SHORT lane, one journaled
OVERNIGHT_HOLD_DECISION per open position, evaluated in this order:

  1. earnings next session (when known)      -> EXIT (gap risk trumps all)
  2. unrealized >= +0.3R                     -> HOLD (winners get the night)
  3. age < 1 session AND realized_fraction
     of predicted move < 0.5                 -> HOLD (thesis needs time)
  4. otherwise                               -> EXIT (stale flat positions
                                                don't earn overnight risk)

LONG lane: default HOLD, no decision rows (holding IS the strategy).
Exits are limit-at-bid; unfilled orders reprice at 15:55 (one step). Still
unfilled at close -> the DAY order expires, the position holds overnight
with its catastrophe stop intact, journaled as OVERNIGHT_FORCED_HOLD.
The pure decision function is separable for the test matrix.
"""
from __future__ import annotations

from typing import Optional


def overnight_decision(unrealized_r: float, session_age: int,
                       realized_fraction: float,
                       earnings_next_session: Optional[bool],
                       cfg: dict) -> tuple[str, str]:
    """(HOLD|EXIT, rule_tag). realized_fraction = fraction of the predicted
    move achieved at the mark; earnings_next_session None = unknown (D7)."""
    if earnings_next_session:
        return "EXIT", "earnings_next_session"
    if unrealized_r >= float(cfg["hold_min_unrealized_R"]):
        return "HOLD", "unrealized_R_threshold"
    if session_age < int(cfg["young_max_age_sessions"]) and \
            realized_fraction < float(cfg["young_max_realized_fraction"]):
        return "HOLD", "young_position"
    return "EXIT", "stale_flat"


def realized_move_fraction(mark: float, avg_entry: float,
                           magnitude_est: float) -> float:
    if magnitude_est <= 0:
        return 0.0
    return ((mark - avg_entry) / avg_entry) / magnitude_est

