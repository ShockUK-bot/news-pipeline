"""A12 gated auto execution (v0.21.0). Pure decision: may THIS verdict be
executed by code without the operator? Doctrine (baseline rule 12, phase 5):
narrow first. Only the subset A11 showed to be right (the three saves were
all high urgency watch-list hits or entry-story corrections) is admitted;
everything else stays advisory. C4 performs the exit (guard_exit_pass),
A12 only arms it."""
from __future__ import annotations


def auto_execute_gate(verdict, item: dict, cfg: dict | None) -> tuple[bool, str]:
    """(execute, why). cfg is a12.yaml `auto_execute`."""
    c = cfg or {}
    if not c.get("enabled", False):
        return False, "auto_execute disabled"
    action = str(verdict.recommended_action).lower()
    if action not in [str(a).lower() for a in (c.get("actions") or ["exit"])]:
        return False, f"action {action} not auto-executed"
    if str(verdict.urgency).lower() not in [str(u).lower() for u in (c.get("urgencies") or ["high"])]:
        return False, f"urgency {verdict.urgency} not auto-executed"
    if c.get("require_watch_hit_or_correction", True):
        hit = bool(verdict.watch_hits)
        corr = bool(item.get("is_correction", False))
        if not (hit or corr):
            return False, "no watch-list hit and not a correction"
        return True, ("watch-list hit: " + "; ".join(verdict.watch_hits)[:160]) if hit else "correction of the entry story"
    return True, f"{action} at {verdict.urgency} urgency"


def arm_block(guard_decision_id: int, why: str, armed_ts: str) -> dict:
    return {"reason": why, "decision_id": guard_decision_id, "armed_ts": armed_ts, "armed_by": "A12"}
