"""A3 Risk/PM service (Phase 4).

Consumes signal.risk (GatePass). Discretion first (bounded LLM adjustment of
k / realization_fraction / time_window within config bands — invalid or
failed output falls back to profile defaults, journaled; the trade never
blocks on the model). Then the deterministic sizing chain. Then ONE
TRANSACTION: RISK decision + intents row + exec.intent enqueue.

intent_id = sha256(signal_id:revision:config_version)[:24] — crash-replay of
the same gated signal can never double-submit, even across config changes
(a new config version is deliberately a new intent).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal as _signal
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.clock import utcnow
from common.config import config_path, load_yaml
from common.contracts import envelope
from common.db import get_pool, close_pool
from common.journal import (active_config_version, register_config_version,
                            write_decision)
from common.log import get_logger, kv
from common.queue import ack, claim, enqueue, fail, wait_for_message
from c1_ingestion.heartbeat import set_health
from a1_triage.backends import get_backend
from router.facts import _schedule_cache

from .sizing import (SizingInputs, hard_gates, open_risk_dollars,
                     size_entry)

log = get_logger("a3.service")

IN_QUEUE = "signal.risk"
OUT_QUEUE = "exec.intent"
CONSUMER = f"a3-{os.getpid()}"
CONTRACT_INTENT = "exec.intent/1"


# ---------------------------------------------------------------------------
# Bounded discretion
# ---------------------------------------------------------------------------

class RiskAdjustments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    k: float
    realization_fraction: float
    time_window_sessions: int
    reason: str = Field(min_length=1, max_length=300)


def validate_adjustments(raw: str, bands: dict) -> RiskAdjustments:
    data = json.loads(raw)
    adj = RiskAdjustments(**data)
    lo, hi = bands["k"]
    if not lo <= adj.k <= hi:
        raise ValueError(f"k {adj.k} outside band [{lo},{hi}]")
    lo, hi = bands["realization_fraction"]
    if not lo <= adj.realization_fraction <= hi:
        raise ValueError(f"realization_fraction {adj.realization_fraction} outside band")
    lo, hi = bands["time_window_sessions"]
    if not lo <= adj.time_window_sessions <= hi:
        raise ValueError(f"time_window_sessions {adj.time_window_sessions} outside band")
    return adj


def adjustments_schema() -> dict:
    return RiskAdjustments.model_json_schema()


DISCRETION_PROMPT = """\
You are the risk sizing adjuster in a long-only news pipeline. Given the
thesis and gate confirmation numbers, choose within the allowed bands:
- k: stop width multiplier on ATR(14). Wider (higher k) for volatile/gappy
  setups or lower confidence; tighter for clean high-confidence confirmations.
- realization_fraction: fraction of the predicted move at which to scale out.
- time_window_sessions: sessions to allow before the time stop.
Bands: k {k_band}, realization_fraction {rf_band}, time_window_sessions {tw_band}.
Defaults if unsure: k={k_default}, realization_fraction={rf_default},
time_window={tw_default}. Respond ONLY with JSON: {{"k": .., 
"realization_fraction": .., "time_window_sessions": .., "reason": ".."}}"""


# ---------------------------------------------------------------------------
# Context gathering (all reads; A3 never calls the broker)
# ---------------------------------------------------------------------------

async def read_controls() -> dict:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT key, value FROM journal.control")
        rows = await cur.fetchall()
    return {k: v for k, v in rows}


async def portfolio_state() -> tuple[dict, float]:
    """(open heat per lane from CURRENT stops, deployed notional)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT horizon, qty_open, avg_entry,
                      (exit_policy->'initial_stop'->>'price')::numeric
               FROM journal.positions WHERE status='OPEN'""")
        rows = await cur.fetchall()
    heat = {"SHORT": 0.0, "LONG": 0.0}
    notional = 0.0
    for horizon, qty_open, avg_entry, stop in rows:
        heat[horizon] += open_risk_dollars(qty_open, float(avg_entry),
                                           float(stop or 0))
        notional += qty_open * float(avg_entry)
    return heat, notional


async def trades_today() -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT count(*) FROM journal.intents
               WHERE side='BUY' AND ts::date = (now() AT TIME ZONE 'UTC')::date
                 AND status NOT IN ('REJECTED')""")
        return (await cur.fetchone())[0]


async def ticker_halted(ticker: str) -> bool:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT count(*) FROM journal.position_events pe
               JOIN journal.positions p USING (position_id)
               WHERE p.ticker=%s AND pe.event_type='HALT_FROZEN'
                 AND NOT EXISTS (SELECT 1 FROM journal.position_events r
                                 WHERE r.position_id=pe.position_id
                                   AND r.event_type='HALT_RESUMED'
                                   AND r.ts > pe.ts)""", (ticker,))
        return (await cur.fetchone())[0] > 0


def minutes_to_close(now: datetime) -> Optional[int]:
    import pandas_market_calendars as mcal
    day_key = now.strftime("%Y-%m-%d")
    if day_key not in _schedule_cache:
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=day_key, end_date=day_key)
        _schedule_cache[day_key] = None if sched.empty else (
            sched.iloc[0]["market_open"].to_pydatetime(),
            sched.iloc[0]["market_close"].to_pydatetime())
    win = _schedule_cache[day_key]
    if win is None or not (win[0] <= now < win[1]):
        return None
    return int((win[1] - now).total_seconds() // 60)


async def earnings_next_sessions(ticker: str) -> Optional[int]:
    """v0.10.0: live lookup against news.earnings_calendar (D7 P1 source
    landed). Defensive by contract — any error degrades to None, which
    journals the EARNINGS_UNKNOWN flag exactly as before the source
    existed. The sizing gate is unchanged: <= blackout sessions -> VETO
    EARNINGS_BLACKOUT for profiles with earnings_blackout_exit."""
    from c1_ingestion.earnings import earnings_next_sessions as _lookup
    return await _lookup(ticker)


async def thesis_decision_id(signal_id: str) -> Optional[int]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT decision_id FROM journal.decisions
               WHERE signal_id=%s AND stage='ANALYST' AND action='THESIS'
               ORDER BY ts DESC LIMIT 1""", (signal_id,))
        row = await cur.fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class A3Service:
    def __init__(self, cfg: dict, profiles: dict, backend=None, now_fn=None):
        self.capital = cfg["capital"]
        self.limits = cfg["limits"]
        self.profiles = profiles["profiles"]
        self.bands = profiles["discretion_bands"]
        self.backend = backend or get_backend(cfg["model"])
        self.now_fn = now_fn or utcnow

    def profile_for(self, horizon: str) -> tuple[str, dict]:
        name = "short_term_v1" if horizon == "SHORT" else "long_term_v1"
        return name, self.profiles[name]

    async def discretion(self, thesis: dict, gate: dict,
                         profile: dict) -> tuple[RiskAdjustments, bool]:
        """Returns (adjustments, model_used). Any failure -> profile defaults."""
        defaults = RiskAdjustments(
            k=float(profile["initial_stop"]["k"]),
            realization_fraction=float(profile["realization"]["target_fraction"]),
            time_window_sessions=int(thesis["expected_move_window"].split("_")[0])
            if thesis["expected_move_window"].endswith("sessions") else 2,
            reason="profile defaults")
        prompt = DISCRETION_PROMPT.format(
            k_band=self.bands["k"], rf_band=self.bands["realization_fraction"],
            tw_band=self.bands["time_window_sessions"],
            k_default=defaults.k, rf_default=defaults.realization_fraction,
            tw_default=defaults.time_window_sessions)
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(
                        {"thesis": thesis, "gate_numbers": {
                            k_: gate.get(k_) for k_ in
                            ("pct_move", "vol_mult", "minutes", "rule")}})}]
        try:
            reply = await self.backend.complete(messages, adjustments_schema())
            return validate_adjustments(reply.text, self.bands), True
        except Exception as e:
            log.warning("discretion fallback to defaults",
                        extra=kv(error=repr(e)[:150]))
            defaults.reason = f"fallback: {repr(e)[:120]}"
            return defaults, False

    def materialize_exit_policy(self, profile_name: str, profile: dict,
                                adj: RiskAdjustments, limit_price: float,
                                atr: float, thesis: dict) -> dict:
        stop_price = round(limit_price - adj.k * atr, 2)
        cat_price = round(limit_price - profile["catastrophe"]["k"] * atr, 2)
        return {
            "profile": profile_name,
            "initial_stop": {"method": "atr", "k": adj.k, "price": stop_price},
            "catastrophe_stop_broker": {"k": profile["catastrophe"]["k"],
                                        "price": cat_price},
            "breakeven_at_R": profile["breakeven_at_R"],
            "trail": dict(profile["trail"]),
            "time_stop": ({"window": f"{adj.time_window_sessions}_sessions",
                           "min_progress_R": profile["time_stop"]["min_progress_R"]}
                          if profile.get("time_stop") else None),
            "realization": {"target_fraction": adj.realization_fraction,
                            "action": profile["realization"]["action"]},
            "machine_invalidations": thesis["invalidation"]["machine_checkable"],
            "news_invalidations": thesis["invalidation"]["news_checkable"],
            "earnings_blackout_exit": profile["earnings_blackout_exit"],
            "overnight_hold": profile["overnight_hold"],
            "magnitude_est": thesis["magnitude_est"],
            "atr_14": atr,
        }

    async def handle(self, msg) -> None:
        body = msg.payload.get("body") or {}
        thesis = body.get("thesis") or {}
        gate = body.get("gate") or {}
        snapshot = gate.get("snapshot") or {}
        trace = msg.payload.get("envelope", {}).get("trace", {})
        signal_id = trace.get("signal_id")
        item_id = trace.get("item_id")
        revision = int(trace.get("revision") or 1)
        if not signal_id or not thesis.get("ticker") or not snapshot:
            raise ValueError(f"malformed GatePass ({msg.dedup_key})")
        ticker = thesis["ticker"]
        horizon = thesis["horizon"]
        profile_name, profile = self.profile_for(horizon)

        controls = await read_controls()
        heat, deployed = await portfolio_state()

        effective_capital = min(
            float(controls.get("broker_equity", "0") or 0),
            float(controls.get("trading_capital", "0") or 0))
        inp = SizingInputs(
            effective_capital=effective_capital,
            settled_cash=float(controls.get("settled_cash", "0") or 0),
            ref_price=float(snapshot["ref_price"]), bid=float(snapshot["bid"]),
            ask=float(snapshot["ask"]),
            spread_bps=float(snapshot["spread_bps"]),
            atr_14=snapshot.get("atr_14") and float(snapshot["atr_14"]),
            adv_20d=snapshot.get("adv_20d") and float(snapshot["adv_20d"]),
            open_heat=heat, deployed_notional=deployed,
            trades_today=await trades_today(),
            ticker_halted=await ticker_halted(ticker),
            kill_switch=controls.get("kill_switch") == "1",
            breaker=controls.get("drawdown_breaker") == "1",
            block_entries=controls.get("block_entries") == "1",
            max_trades_per_day=int(controls.get(
                "max_trades_per_day",
                str(self.limits["max_trades_per_day_default"]))),
            minutes_to_close=minutes_to_close(self.now_fn()),
            earnings_next_sessions=await earnings_next_sessions(ticker),
        )

        # hard gates BEFORE the model call: no tokens burned under a kill
        # switch, and operational vetoes dominate in the journal
        gate_veto, _, _ = hard_gates(inp, self.limits, profile)
        if gate_veto is not None:
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                ticker=ticker, stage="RISK", agent="A3", action="VETO",
                veto_reason=gate_veto.veto_reason,
                payload={"sizing": gate_veto.numbers, "flags": gate_veto.flags,
                         "effective_capital": effective_capital},
                reason=gate_veto.veto_reason,
                regime_id=body.get("regime_id"))
            log.info("risk VETO", extra=kv(signal_id=signal_id,
                                           reason=gate_veto.veto_reason))
            return

        # thesis lineage is load-bearing (positions.thesis_decision_id NOT
        # NULL): a GatePass whose ANALYST decision can't be found must not size
        tdid = await thesis_decision_id(signal_id)
        if tdid is None:
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                ticker=ticker, stage="RISK", agent="A3", action="VETO",
                veto_reason="NO_THESIS_LINEAGE",
                payload={"detail": "no ANALYST THESIS decision for signal"},
                reason="missing thesis lineage",
                regime_id=body.get("regime_id"))
            log.warning("risk VETO no thesis lineage",
                        extra=kv(signal_id=signal_id))
            return

        adj, model_used = await self.discretion(thesis, gate, profile)
        result = size_entry(inp, self.capital, self.limits, profile,
                            horizon, adj.k)

        payload = {"sizing": result.numbers, "flags": result.flags,
                   "adjustments": adj.model_dump(),
                   "model_used": model_used,
                   "effective_capital": effective_capital}

        if result.verdict == "VETO":
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                ticker=ticker, stage="RISK", agent="A3", action="VETO",
                veto_reason=result.veto_reason, payload=payload,
                reason=f"{result.veto_reason}",
                model_id=self.backend.model_id if model_used else None,
                regime_id=body.get("regime_id"))
            log.info("risk VETO", extra=kv(signal_id=signal_id,
                                           reason=result.veto_reason))
            return

        config_version = active_config_version()
        intent_id = hashlib.sha256(
            f"{signal_id}:{revision}:{config_version}".encode()).hexdigest()[:24]
        exit_policy = self.materialize_exit_policy(
            profile_name, profile, adj, result.limit_price,
            float(snapshot["atr_14"]), thesis)

        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=signal_id, item_id=item_id, item_revision=revision,
                    ticker=ticker, stage="RISK", agent="A3", action="SIZE",
                    payload={**payload, "intent_id": intent_id,
                             "exit_policy": exit_policy,
                             "thesis_decision_id": tdid},
                    reason=adj.reason,
                    model_id=self.backend.model_id if model_used else None,
                    regime_id=body.get("regime_id"), conn=conn)
                await conn.execute(
                    """INSERT INTO journal.intents
                       (intent_id, decision_id, ticker, side, qty, limit_price,
                        gate_snapshot, exit_policy, horizon, effective_capital,
                        risk_budget, status, config_version)
                       VALUES (%s,%s,%s,'BUY',%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
                       ON CONFLICT (intent_id) DO NOTHING""",
                    (intent_id, decision_id, ticker, result.qty,
                     result.limit_price, json.dumps(snapshot),
                     json.dumps(exit_policy), horizon, effective_capital,
                     result.risk_budget, config_version))
                out = envelope(CONTRACT_INTENT, "A3", signal_id, item_id,
                               revision, {
                                   "intent_id": intent_id,
                                   "ticker": ticker, "side": "BUY",
                                   "qty": result.qty,
                                   "limit_price": result.limit_price,
                                   "exit_policy": exit_policy,
                                   "gate_snapshot": snapshot,
                                   "horizon": horizon,
                                   "thesis_decision_id": tdid,
                                   "effective_capital": effective_capital,
                                   "risk_budget": result.risk_budget})
                out["envelope"]["trace"]["decision_id"] = decision_id
                await enqueue(OUT_QUEUE, intent_id, out, conn=conn)

        log.info("intent", extra=kv(signal_id=signal_id, ticker=ticker,
                                    qty=result.qty, limit=result.limit_price,
                                    risk=result.actual_risk,
                                    intent_id=intent_id))


async def consume_loop(svc: A3Service, stop: asyncio.Event) -> None:
    await set_health("risk", "OK", f"consuming {IN_QUEUE}")
    while not stop.is_set():
        msg = await claim(IN_QUEUE, CONSUMER)
        if msg is None:
            try:
                await asyncio.wait_for(wait_for_message(IN_QUEUE, timeout_secs=5.0), 6.0)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await svc.handle(msg)
            await ack(msg.msg_id)
        except Exception as e:
            log.error("message failed", extra=kv(msg_id=msg.msg_id, error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def main() -> None:
    cfg = load_yaml(config_path("risk.yaml"))
    profiles = load_yaml(config_path("exit_profiles.yaml"))
    await register_config_version("a3 risk service startup")
    svc = A3Service(cfg, profiles)
    log.info("A3 up", extra=kv(consumer=CONSUMER))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await consume_loop(svc, stop)
    await set_health("risk", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

