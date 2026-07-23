"""C4 exit engine loop (Phase 4 chunk 2).

PositionEngine owns the live per-position state that must survive across
bars: compiled MIP monitors (persistence streaks are stateful) and halt
freeze status. One engine instance runs inside the C4 service; tests drive
step()/overnight_pass() directly with synthetic bars and a pinned clock.

Responsibilities per bar: mark-to-market cache -> halt heuristic ->
MIP on_bar (exit fires feed the evaluator; tighten_stop/alert_guard fires
become ratchets/guard events) -> evaluate_on_bar -> apply actions through
mechanics (never unprotected beyond the configured window).
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from common.clock import utcnow
from common.db import get_pool, jb
from common.invalidation_dsl import ArmContext, Bar, compile_predicate
from common.log import get_logger, kv

from .exits import ExitAction, evaluate_on_bar, policy_state
from .mechanics import execute_exit
from .overnight import overnight_decision, realized_move_fraction
from .state import open_positions, position_event

log = get_logger("c4.engine")
ET = ZoneInfo("America/New_York")

_session_cache: dict = {}


def sessions_between(opened_ts: datetime, now: datetime) -> int:
    """Completed sessions since entry (0 on the entry session)."""
    import pandas_market_calendars as mcal
    key = (opened_ts.date().isoformat(), now.date().isoformat())
    if key not in _session_cache:
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=key[0], end_date=key[1])
        _session_cache[key] = max(len(sched) - 1, 0)
    return _session_cache[key]


class PositionEngine:
    def __init__(self, broker, now_fn=None, unprotected_max_secs: float = 45.0,
                 poll_sleep: float = 1.0, halt_stale_min: float = 10.0,
                 session_age_fn=None):
        self.broker = broker
        self.now_fn = now_fn or utcnow
        self.unprotected_max_secs = unprotected_max_secs
        self.poll_sleep = poll_sleep
        self.halt_stale_min = halt_stale_min
        self.session_age_fn = session_age_fn or sessions_between
        self.monitors: dict[int, list] = {}       # position_id -> predicates
        self.frozen: set[int] = set()
        self.last_bar_ts: dict[int, datetime] = {}

    # ------------------------------------------------------------------ arming
    async def arm(self, pos: dict) -> None:
        """Compile machine invalidations once per position; journal the
        compiled literal forms (INVALIDATION_ARMED)."""
        pid = pos["position_id"]
        if pid in self.monitors:
            return
        policy = pos["exit_policy"]
        specs = policy.get("machine_invalidations") or []
        compiled = []
        armed_forms = []
        ctx = ArmContext(entry_price=float(pos["avg_entry"]),
                         initial_stop=float(pos["initial_stop"]),
                         r_unit=float(pos["r_unit"]),
                         prenews_price=policy.get("prenews_price")
                         and float(policy["prenews_price"]),
                         atr_14=policy.get("atr_14") and float(policy["atr_14"]),
                         mark=pos.get("last_price") and float(pos["last_price"]))
        for spec in specs:
            if isinstance(spec, str):
                spec = {"std": spec}
            try:
                p = compile_predicate(spec, ctx)
                compiled.append(p)
                armed_forms.append(p.compiled_form)
            except Exception as e:
                await position_event(pid, "INVALIDATION_ARMED", "C4",
                                     new_value={"spec": spec},
                                     detail=f"ARM FAILED: {repr(e)[:150]}")
                log.warning("predicate arm failed",
                            extra=kv(position_id=pid, error=repr(e)[:150]))
        self.monitors[pid] = compiled
        if armed_forms:
            await position_event(pid, "INVALIDATION_ARMED", "C4",
                                 new_value={"predicates": armed_forms},
                                 detail=f"{len(armed_forms)} armed")

    # -------------------------------------------------------------------- bars
    async def step(self, pos: dict, bar: dict) -> list[str]:
        """Process one minute bar for one open position. Returns applied
        action descriptions (test/observability)."""
        pid = pos["position_id"]
        now = self.now_fn()
        await self.arm(pos)
        self.last_bar_ts[pid] = now
        if pid in self.frozen:
            self.frozen.discard(pid)
            await position_event(pid, "HALT_RESUMED", "C4",
                                 detail="bar flow resumed")

        await self._mark(pid, bar["close"])
        pos = {**pos, "last_price": bar["close"]}

        # MIP monitors
        mip_bar = Bar(ts=int(bar.get("ts", now.timestamp())),
                      tf=bar.get("tf", "1m"),
                      open=bar["open"], high=bar["high"], low=bar["low"],
                      close=bar["close"], vwap=bar.get("vwap", bar["close"]),
                      volume_ratio=bar.get("volume_ratio", 1.0))
        exit_fires, extra_actions = [], []
        for p in self.monitors.get(pid, []):
            fire = p.on_bar(mip_bar)
            if fire is None:
                continue
            await position_event(pid, "INVALIDATION_FIRED", "C4",
                                 new_value={"predicate": fire.predicate_id,
                                            "action": fire.action},
                                 detail=fire.detail[:200])
            if fire.action.get("type") == "exit":
                exit_fires.append(fire)
            elif fire.action.get("type") == "tighten_stop":
                extra_actions.append(self._tighten_from_fire(pos, fire))
            else:                                     # alert_guard
                extra_actions.append(ExitAction(
                    "EVENT", "", 0, event_type="GUARD_ACTION",
                    reason=f"alert_guard: {fire.predicate_id}"))

        session_age = self.session_age_fn(pos["opened_ts"], now)
        minutes_open = (now - pos["opened_ts"]).total_seconds() / 60.0 \
            if pos.get("opened_ts") else None
        actions = evaluate_on_bar(pos, bar, session_age, exit_fires,
                                  minutes_open=minutes_open)
        actions.extend(a for a in extra_actions if a is not None)
        return await self._apply(pos, actions, bar)

    def _tighten_from_fire(self, pos: dict, fire) -> Optional[ExitAction]:
        to = fire.action.get("to", {})
        policy = pos["exit_policy"]
        state = policy_state(policy, float(pos["avg_entry"]))
        if to == {"ref": "breakeven"}:
            new_stop = round(float(pos["avg_entry"]), 2)
            basis = "breakeven"
        else:
            atr = float(policy["atr_14"])
            new_stop = round(state["hwm"] - float(to["atr_k"]) * atr, 2)
            basis = "trail"
        if new_stop <= state["current_stop"]:
            return None                               # tighten-only
        return ExitAction("SET_STOP", "", 0, new_stop=new_stop,
                          new_basis=basis,
                          reason=f"MIP tighten_stop {fire.predicate_id}")

    async def check_halt(self, pos: dict) -> bool:
        """True if the position is (now) frozen: no bar within the stale
        window during RTH. LULD heuristic-only until SIP (D7)."""
        pid = pos["position_id"]
        last = self.last_bar_ts.get(pid)
        if last is None:
            return False
        stale_min = (self.now_fn() - last).total_seconds() / 60.0
        if stale_min > self.halt_stale_min and pid not in self.frozen:
            self.frozen.add(pid)
            await position_event(pid, "HALT_FROZEN", "C4",
                                 detail=f"no bar for {stale_min:.1f}min — "
                                        f"halt heuristic; evaluations frozen")
            log.warning("halt heuristic froze position",
                        extra=kv(position_id=pid, ticker=pos["ticker"]))
        return pid in self.frozen

    # ------------------------------------------------------------------- apply
    async def _apply(self, pos: dict, actions: list[ExitAction],
                     bar: dict) -> list[str]:
        applied = []
        for a in actions:
            if a.kind in ("EXIT", "SCALE_OUT"):
                bid = bar.get("bid") or round(bar["close"] * 0.999, 2)
                outcome = await execute_exit(
                    self.broker, pos, a.qty, a.layer, a.reason, bid,
                    self.now_fn, self.unprotected_max_secs, self.poll_sleep)
                applied.append(f"{a.kind}:{a.layer}:{outcome}")
                if a.kind == "EXIT" or outcome == "CATASTROPHE_FILLED":
                    self.monitors.pop(pos["position_id"], None)
                    break                             # position closed
            elif a.kind == "SET_STOP":
                await self._ratchet(pos, a)
                applied.append(f"SET_STOP:{a.new_basis}:{a.new_stop}")
            elif a.kind == "EVENT":
                if a.event_type == "POSITION_REVIEW":
                    await position_event(pos["position_id"], "SCALE_OUT", "C4",
                                         detail=f"REVIEW_FLAG: {a.reason}",
                                         new_value={"review": True})
                    applied.append("EVENT:REVIEW")
                elif a.event_type == "GUARD_ACTION":
                    await position_event(pos["position_id"], "GUARD_ACTION",
                                         "C4", detail=a.reason)
                    applied.append("EVENT:GUARD")
                if a.new_hwm is not None:
                    await self._update_policy(pos, {"hwm": a.new_hwm})
        return applied

    async def _ratchet(self, pos: dict, a: ExitAction) -> None:
        policy = pos["exit_policy"]
        state = policy_state(policy, float(pos["avg_entry"]))
        event_type = {"breakeven": "BREAKEVEN_MOVED",
                      "trail": "TRAIL_UPDATED"}.get(a.new_basis,
                                                    "STOP_TIGHTENED")
        updates = {"current_stop": a.new_stop, "stop_basis": a.new_basis}
        if a.new_hwm is not None:
            updates["hwm"] = a.new_hwm
        await self._update_policy(pos, updates)
        r_prog = ((float(pos.get("last_price") or pos["avg_entry"]))
                  - float(pos["avg_entry"])) / float(pos["r_unit"])
        await position_event(pos["position_id"], event_type, "C4",
                             old_value={"stop": state["current_stop"],
                                        "basis": state["stop_basis"]},
                             new_value={"stop": a.new_stop,
                                        "basis": a.new_basis},
                             r_progress=round(r_prog, 3), detail=a.reason)

    async def _update_policy(self, pos: dict, updates: dict) -> None:
        pos["exit_policy"].update(updates)
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """UPDATE journal.positions SET exit_policy=%s
                   WHERE position_id=%s""",
                (jb(pos["exit_policy"]), pos["position_id"]))

    async def _mark(self, position_id: int, price: float) -> None:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """UPDATE journal.positions SET last_price=%s, last_price_ts=%s
                   WHERE position_id=%s""",
                (price, self.now_fn(), position_id))

    async def session_close_pass(self, daily_bar_fn) -> None:
        """After the close: feed each open position its completed session bar
        so session-tf MIP predicates (e.g. close_below_prenews) can evaluate.
        daily_bar_fn(ticker) -> {open,high,low,close} of the finished session."""
        for pos in await open_positions():
            b = await daily_bar_fn(pos["ticker"])
            if not b:
                continue
            await self.step(pos, {**b, "tf": "session"})

    # --------------------------------------------------------------- force-flat
    async def force_flat_pass(self) -> list[str]:
        """v0.12.1 — the scalp lane's hard no-overnight rule. Any open
        position whose policy says `overnight_hold: force_flat` is market-
        exited once ET reaches its `force_flat_time_et` (default 15:50).
        Pure code: runs even if every model is down; no discretion, no
        A6-lite pass, journaled FORCE_FLAT. Aggressive limit (bid minus a
        step) — the point is to be flat, not to price-improve."""
        now_et = self.now_fn().astimezone(ET)
        hhmm = now_et.strftime("%H:%M")
        flattened = []
        for pos in await open_positions():
            policy = pos["exit_policy"]
            if policy.get("overnight_hold") != "force_flat":
                continue
            if hhmm < str(policy.get("force_flat_time_et", "15:50")):
                continue
            mark = float(pos.get("last_price") or pos["avg_entry"])
            bid = round(mark * 0.997, 2)
            await position_event(
                pos["position_id"], "FORCE_FLAT", "C4",
                new_value={"time_et": hhmm, "mark": mark},
                detail=f"force-flat @ {hhmm} ET (no-overnight scalp rule)")
            outcome = await execute_exit(
                self.broker, pos, int(pos["qty_open"]), "FORCE_FLAT",
                f"force_flat {hhmm} ET", bid, self.now_fn,
                self.unprotected_max_secs, self.poll_sleep)
            self.monitors.pop(pos["position_id"], None)
            flattened.append(f"{pos['ticker']}:{outcome}")
            log.info("force flat", extra=kv(ticker=pos["ticker"],
                                            outcome=outcome))
        return flattened

    # ---------------------------------------------------------------- overnight
    async def overnight_pass(self, cfg: dict, earnings_fn=None,
                             pass_label: str = "15:45") -> list[tuple]:
        """D1: one decision per open SHORT position; EXIT -> limit at bid.
        Run at 15:45 ET and again at 15:55 (reprice pass) — the second pass
        re-attempts only positions still open. Returns [(ticker, decision,
        rule)] for tests."""
        results = []
        for pos in await open_positions():
            if pos["horizon"] != "SHORT":
                continue
            policy = pos["exit_policy"]
            if policy.get("overnight_hold") == "force_flat":
                continue        # v0.12.1: force_flat_pass owns these (belt+braces)
            avg_entry = float(pos["avg_entry"])
            mark = float(pos.get("last_price") or avg_entry)
            unrealized_r = (mark - avg_entry) / float(pos["r_unit"])
            age = self.session_age_fn(pos["opened_ts"], self.now_fn())
            frac = realized_move_fraction(mark, avg_entry,
                                          float(policy.get("magnitude_est") or 0))
            earn = await earnings_fn(pos["ticker"]) if earnings_fn else None
            decision, rule = overnight_decision(unrealized_r, age, frac,
                                                earn, cfg)
            await position_event(
                pos["position_id"], "OVERNIGHT_HOLD_DECISION", "C4",
                new_value={"decision": decision, "rule": rule,
                           "unrealized_R": round(unrealized_r, 3),
                           "session_age": age,
                           "realized_fraction": round(frac, 3),
                           "pass": pass_label},
                r_progress=round(unrealized_r, 3),
                detail=f"{decision} ({rule}) @ {pass_label}")
            if decision == "EXIT":
                bid = round(mark * 0.999, 2)
                outcome = await execute_exit(
                    self.broker, pos, int(pos["qty_open"]), "OVERNIGHT",
                    f"D1 {rule}", bid, self.now_fn,
                    self.unprotected_max_secs, self.poll_sleep)
                if outcome == "REINSTATED" and pass_label == "15:55":
                    await position_event(
                        pos["position_id"], "OVERNIGHT_HOLD_DECISION", "C4",
                        new_value={"decision": "FORCED_HOLD"},
                        detail="OVERNIGHT_FORCED_HOLD: unfilled after reprice; "
                               "holding with catastrophe intact")
            results.append((pos["ticker"], decision, rule))
        return results

