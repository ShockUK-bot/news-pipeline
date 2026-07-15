"""The four routing rules (queue-contracts-spec §6), deterministic, in order,
as a PURE function — no I/O, trivially testable:

  1. position_ids non-empty -> signal.guard (priority 0), IN ADDITION to
     whatever normal routing produces.
  2. material=false -> DISCARD (journal only); stop.
     v0.4.7: material=true with confidence < min_confidence also DISCARDs
     (the threshold lever; ships at 0.0 = inactive until distributions are
     observed — config-values discipline, baseline §14).
  3. material=true, no ticker mappable -> signal.thesis (A5 lane);
     never intraday.
  4. market_open -> signal.analyst; else signal.overnight ordered by
     priority_score (queue priority ascending: overnight_base - score).
"""
from __future__ import annotations

from dataclasses import dataclass

from a1_triage.schema import TriageOutput
from .facts import RoutingFacts

GUARD_QUEUE = "signal.guard"
THESIS_QUEUE = "signal.thesis"
ANALYST_QUEUE = "signal.analyst"
OVERNIGHT_QUEUE = "signal.overnight"


@dataclass(frozen=True)
class Route:
    queue: str
    priority: int


@dataclass(frozen=True)
class RoutingDecision:
    action: str                 # ESCALATE | DISCARD
    routes: tuple[Route, ...]   # possibly empty (DISCARD)


def route(triage: TriageOutput, facts: RoutingFacts,
          overnight_base: int = 50, min_confidence: float = 0.0) -> RoutingDecision:
    routes: list[Route] = []

    # Rule 1 — guard fan-out happens regardless of the outcome below.
    if facts.position_ids:
        routes.append(Route(GUARD_QUEUE, 0))

    # Rule 2 — not material: journal DISCARD, stop. (Guard fan-out above still
    # applies: a correction touching a held name must reach A12 even if A1
    # scores the item itself immaterial.) v0.4.7: a material verdict below the
    # confidence floor is treated as not material; 0.0 disables the lever.
    if not triage.material or triage.confidence < min_confidence:
        return RoutingDecision("DISCARD", tuple(routes))

    # Rule 3 — material but no ticker: thesis lane, never intraday.
    if not triage.tickers:
        routes.append(Route(THESIS_QUEUE, 100))
        return RoutingDecision("ESCALATE", tuple(routes))

    # Rule 4 — market-open branch.
    if facts.market_open:
        routes.append(Route(ANALYST_QUEUE, 100))
    else:
        routes.append(Route(OVERNIGHT_QUEUE,
                            max(0, overnight_base - facts.priority_score)))
    return RoutingDecision("ESCALATE", tuple(routes))

