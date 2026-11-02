"""A2 deterministic pre-check (v0.30.0, 2026-11-02).

October 2026: the analyst ran 8,354 times and rejected 6,343; about 1,100 of
those rejects said "the stock has not moved since the news" and about 200
"the stock is halted". Both are facts the code knows before the 20 to 30
second model call. During the regular session a news-lane signal whose price
has not moved at least `min_move_pct` from its pre-news reference is DEFERRED
(re-queued, no model call) until it moves or `wait_max_min` minutes have
passed since the news, at which point it is REJECTED deterministically with
a journal row that says so. Outside the regular session (pre-market news,
no bars yet) nothing changes: the analyst runs as before, because the open
handoff is where those items are judged.

Scanner-origin signals are never pre-checked: the move IS the signal.
Pure functions; the service does the journaling and the queue work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PrecheckVerdict:
    action: str                 # "DEFER" | "REJECT"
    reason: str
    delay_secs: float = 0.0
    pct_move: Optional[float] = None
    minutes: Optional[int] = None


def precheck(context: dict, origin: str, in_regular_session: bool,
             cfg: dict | None) -> Optional[PrecheckVerdict]:
    """None = run the model. cfg is a2.yaml's `precheck` block."""
    c = cfg or {}
    if not c.get("enabled", False) or origin != "news":
        return None
    if c.get("rth_only", True) and not in_regular_session:
        return None
    pa = (context or {}).get("price_action") or {}
    pct = pa.get("pct_move_since_news")
    minutes = pa.get("minutes_since_news")
    if pct is None or minutes is None:
        return None                       # unknown is not "unmoved"
    min_move = float(c.get("min_move_pct", 0.005))
    if abs(float(pct)) >= min_move:
        return None
    wait_max = int(c.get("wait_max_min", 60))
    if int(minutes) < wait_max:
        return PrecheckVerdict(
            "DEFER", f"unmoved {float(pct):+.2%} after {int(minutes)} min; "
                     f"re-check in {int(c.get('defer_secs', 300))}s (no model call)",
            delay_secs=float(c.get("defer_secs", 300)),
            pct_move=float(pct), minutes=int(minutes))
    return PrecheckVerdict(
        "REJECT", f"pre-check: unmoved {float(pct):+.2%} {int(minutes)} min "
                  f"after the news (threshold {min_move:.2%}, window {wait_max} min); "
                  f"no model call",
        pct_move=float(pct), minutes=int(minutes))
