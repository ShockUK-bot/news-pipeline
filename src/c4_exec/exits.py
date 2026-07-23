"""C4 exit engine — pure per-bar evaluator (Phase 4 chunk 2).

Layers per baseline v0.3 §5, evaluated in strict priority on each bar:
  L1 synthetic hard stop   bar.low <= current_stop -> full exit. Attribution
                           follows stop_basis: initial->STOP, breakeven->
                           BREAKEVEN, trail->TRAIL.
  L5 machine invalidation  compiled MIP predicates fire -> full exit
                           (INVALIDATION). Runs second: an invalidation on the
                           same bar as a stop is moot — the stop already got us.
  L3 time stop             session age >= window AND progress < min_progress_R
                           -> full exit (TIME). Short profile only.
  L4 realization           bar.high >= target -> scale_out_50 (TARGET, partial)
                           or review_flag (EVENT only, long lane).
  L2 ratchets              breakeven move at >= breakeven_at_R; trail from
                           activate_at_R at k x ATR below high-water mark.
                           TIGHTEN-ONLY: a proposed stop below current is
                           discarded, never applied.

The evaluator is pure: (position snapshot, bar, session_age, fired
invalidations) -> ordered actions. All broker mechanics live in mechanics.py;
all persistence in the service. Runtime policy state rides in exit_policy:
current_stop, stop_basis, hwm, scale_out_done.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExitAction:
    kind: str                      # EXIT | SCALE_OUT | SET_STOP | EVENT
    layer: str = ""                # exits.exit_layer vocabulary
    qty: int = 0
    reason: str = ""
    new_stop: Optional[float] = None
    new_basis: Optional[str] = None
    event_type: Optional[str] = None
    new_hwm: Optional[float] = None


STOP_ATTRIBUTION = {"initial": "STOP", "breakeven": "BREAKEVEN",
                    "trail": "TRAIL"}


def policy_state(exit_policy: dict, avg_entry: float) -> dict:
    """Current runtime state with defaults for freshly opened positions."""
    return {
        "current_stop": float(exit_policy.get("current_stop")
                              or exit_policy["initial_stop"]["price"]),
        "stop_basis": exit_policy.get("stop_basis", "initial"),
        "hwm": float(exit_policy.get("hwm") or avg_entry),
        "scale_out_done": bool(exit_policy.get("scale_out_done", False)),
    }


def realization_target(avg_entry: float, exit_policy: dict) -> float:
    rf = float(exit_policy["realization"]["target_fraction"])
    magnitude = float(exit_policy.get("magnitude_est") or 0)
    return round(avg_entry * (1.0 + rf * magnitude), 4)


def evaluate_on_bar(pos: dict, bar: dict, session_age: int,
                    fired_invalidations: list | None = None,
                    minutes_open: Optional[float] = None
                    ) -> list[ExitAction]:
    """pos: positions row as dict (exit_policy already parsed).
    bar: {ts, open, high, low, close} floats.
    session_age: completed sessions since entry (0 = entry session).
    fired_invalidations: Fire objects from the compiled MIP monitors for
    this bar (the caller runs the DSL; the evaluator stays pure).
    minutes_open (v0.12.1): minutes since entry — feeds minutes-based time
    stops (scalp_v1's `window_minutes`); session-based windows ignore it."""
    policy = pos["exit_policy"]
    avg_entry = float(pos["avg_entry"])
    r_unit = float(pos["r_unit"])
    qty_open = int(pos["qty_open"])
    state = policy_state(policy, avg_entry)
    actions: list[ExitAction] = []

    progress_r = (bar["close"] - avg_entry) / r_unit if r_unit else 0.0
    new_hwm = max(state["hwm"], bar["high"])

    # ---- L1 synthetic hard stop ------------------------------------------------
    if bar["low"] <= state["current_stop"]:
        layer = STOP_ATTRIBUTION[state["stop_basis"]]
        actions.append(ExitAction("EXIT", layer, qty_open,
                                  reason=f"bar low {bar['low']} <= stop "
                                         f"{state['current_stop']} "
                                         f"({state['stop_basis']})"))
        return actions

    # ---- L5 machine invalidations ----------------------------------------------
    if fired_invalidations:
        f = fired_invalidations[0]
        actions.append(ExitAction("EXIT", "INVALIDATION", qty_open,
                                  reason=f"{f.predicate_id}: {f.detail}"[:200]))
        return actions

    # ---- L3 time stop ------------------------------------------------------------
    ts_cfg = policy.get("time_stop")
    if ts_cfg and "window_minutes" in ts_cfg:
        # v0.12.1 scalp lane: a mover that stops moving has no thesis.
        window_min = int(ts_cfg["window_minutes"])
        if minutes_open is not None and minutes_open >= window_min \
                and progress_r < float(ts_cfg["min_progress_R"]):
            actions.append(ExitAction(
                "EXIT", "TIME", qty_open,
                reason=f"open {minutes_open:.0f}min >= {window_min}min window, "
                       f"progress {progress_r:.2f}R < "
                       f"{ts_cfg['min_progress_R']}R"))
            return actions
    elif ts_cfg:
        window = int(str(ts_cfg["window"]).split("_")[0])
        if session_age >= window and progress_r < float(ts_cfg["min_progress_R"]):
            actions.append(ExitAction(
                "EXIT", "TIME", qty_open,
                reason=f"age {session_age}s >= {window}s window, "
                       f"progress {progress_r:.2f}R < "
                       f"{ts_cfg['min_progress_R']}R"))
            return actions

    # ---- L4 realization ------------------------------------------------------------
    if not state["scale_out_done"]:
        target = realization_target(avg_entry, policy)
        if bar["high"] >= target:
            action = policy["realization"]["action"]
            if action == "scale_out_50":
                half = qty_open // 2
                if half > 0:
                    actions.append(ExitAction("SCALE_OUT", "TARGET", half,
                                              reason=f"high {bar['high']} >= "
                                                     f"target {target}"))
            else:                               # review_flag (long lane)
                actions.append(ExitAction(
                    "EVENT", "TARGET", 0, event_type="POSITION_REVIEW",
                    reason=f"target {target} reached — review flagged"))

    # ---- L2 ratchets (tighten-only) ---------------------------------------------
    # v0.12.1: atr_value = the stop-basis ATR (5-min for scalp_v1); older
    # policies carry only atr_14 (daily), which remains the fallback.
    atr = float(policy.get("atr_value") or policy["atr_14"])
    proposed: Optional[tuple[float, str]] = None
    trail_cfg = policy.get("trail") or {}
    if trail_cfg and progress_r >= float(trail_cfg["activate_at_R"]):
        trail_stop = round(new_hwm - float(trail_cfg["k"]) * atr, 2)
        proposed = (trail_stop, "trail")
    elif progress_r >= float(policy["breakeven_at_R"]) \
            and state["stop_basis"] == "initial":
        proposed = (round(avg_entry, 2), "breakeven")

    if proposed and proposed[0] > state["current_stop"]:
        actions.append(ExitAction("SET_STOP", "", 0,
                                  new_stop=proposed[0], new_basis=proposed[1],
                                  reason=f"{proposed[1]} ratchet to "
                                         f"{proposed[0]} at {progress_r:.2f}R",
                                  new_hwm=new_hwm))
    elif new_hwm > state["hwm"]:
        actions.append(ExitAction("EVENT", "", 0, event_type=None,
                                  new_hwm=new_hwm, reason="hwm update"))
    return actions

