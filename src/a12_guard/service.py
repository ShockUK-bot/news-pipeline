"""A12 Position Guard service (Phase 5, verdict-only — auto-execution OFF).

Consumer of signal.guard (TriagedSignal §6 + routing.position_ids, enqueued
by the router at priority 0 whenever a triaged item touches an open
position — including corrections and items A1 scored immaterial).

Per message:
  1. staleness gate (code): items older than guard.max_age_minutes journal
     GUARD/EXPIRED and stop — no tokens. This is also how the pre-Phase-5
     signal.guard backlog drains on first start.
  2. fetch the item's seen revision from the news store; re-resolve the
     routed position_ids against journal.positions (still OPEN?) — closed
     or unknown positions journal GUARD/NO_POSITION and stop.
  3. analyst-slot probe (+ wake off-hours, same discipline as deployed A13);
     slot down after wake attempt -> GUARD/ALERT_ONLY per baseline §11.2
     (position-touching news degrades to operator notification, never to
     silent loss of the signal).
  4. per open position: build code-computed context, run the guard model
     (grammar-constrained GuardVerdict), one retry, else REJECT; then ONE
     TRANSACTION: GUARD decision row + guard_ledger row.
  5. ack. At-least-once safety: a redelivered message finds its GUARD
     decision rows and skips them (dedup on signal/revision/position).

WHAT THIS SERVICE NEVER DOES (v1, baseline rules 10/12/16):
  no orders, no stop changes, no C4 calls, no queue fan-out. The verdict is
  journaled and surfaced (dashboard tape, guard ledger, health row); acting
  on it is the operator's job until auto-execution is promoted through the
  A9 channel with its own enablement criteria. There is deliberately no
  execution code path here — not a disabled one.

Model slot: Analyst (:8081, shared with A2/A3 discretion/A13). A12 does not
yield to anyone: guard checks and A2 escalations are the slot's priority
tenants (A13 yields to BOTH — its slot.py reads signal.guard depth). Worst
case A12 waits behind one in-flight analyst generation (~60-90s at measured
~12.5 tok/s) — within the guard-verdict latency budget (baseline §2, ≤60s
target, best-effort during contention).
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal
import time

import httpx

from common.config import config_path, load_yaml
from common.clock import parse_ts, utcnow
from common.db import get_pool, jb
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from common.marketdata import get_marketdata
from common.queue import ack, claim, fail, wait_for_message
from c1_ingestion.heartbeat import set_health
from a1_triage.backends import get_backend

from .context import build_guard_context, fetch_thesis, position_pack
from .prompt import build_messages
from .schema import (ACTION_MAP, GuardValidationError, guard_json_schema,
                     validate_guard)
from .wake import ensure_model_up

log = get_logger("a12.service")

IN_QUEUE = "signal.guard"
CONSUMER = f"a12-{os.getpid()}"
HEALTH_COMPONENT = "guard"


async def fetch_item(item_id: str, revision: int) -> dict | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT item_id, revision, is_correction, source, source_tier,
                      source_url, headline, summary, symbols, channels,
                      published_ts, received_ts
               FROM news.news_items WHERE item_id = %s AND revision = %s""",
            (item_id, revision))
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
        item = dict(zip(cols, row))
        item["published_ts"] = item["published_ts"].isoformat()
        item["received_ts"] = item["received_ts"].isoformat()
        return item


async def open_positions(position_ids: list[int]) -> list[dict]:
    if not position_ids:
        return []
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT position_id, ticker, horizon, profile, status, opened_ts,
                      thesis_decision_id, item_id, qty_open, avg_entry,
                      initial_stop, r_unit, exit_policy, realized_pnl
               FROM journal.positions
               WHERE position_id = ANY(%s) AND status = 'OPEN'
               ORDER BY position_id""", (position_ids,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in await cur.fetchall()]


async def _existing_guard_decision(signal_id: str, revision: int,
                                   position_id: int | None) -> bool:
    """Redelivery dedup. position_id=None checks message-level rows
    (EXPIRED / NO_POSITION / ALERT_ONLY)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        if position_id is None:
            cur = await conn.execute(
                """SELECT 1 FROM journal.decisions
                   WHERE stage='GUARD' AND signal_id=%s AND item_revision=%s
                     AND (payload->>'position_id') IS NULL LIMIT 1""",
                (signal_id, revision))
        else:
            cur = await conn.execute(
                """SELECT 1 FROM journal.decisions
                   WHERE stage='GUARD' AND signal_id=%s AND item_revision=%s
                     AND (payload->>'position_id')::bigint = %s LIMIT 1""",
                (signal_id, revision, position_id))
        return (await cur.fetchone()) is not None


async def write_ledger(conn, decision_id: int, position_id: int, item_id: str,
                       verdict) -> None:
    await conn.execute(
        """INSERT INTO journal.guard_ledger
           (decision_id, position_id, item_id, ts, thesis_intact,
            recommended_action, urgency, auto_executed, action_taken)
           VALUES (%s,%s,%s, now(), %s,%s,%s, FALSE, 'JOURNALED')""",
        (decision_id, position_id, item_id, verdict.thesis_intact,
         ACTION_MAP[verdict.recommended_action], verdict.urgency))


class A12Service:
    def __init__(self, cfg: dict, backend=None, md=None):
        self.cfg = cfg
        self.backend = backend or get_backend(cfg["model"])
        self.retries = int(cfg["model"].get("retries_on_invalid", 1))
        self.md = md or get_marketdata()
        self.guard_cfg = cfg.get("guard") or {}
        self.max_age_minutes = float(self.guard_cfg.get("max_age_minutes", 240))
        self.wake_cfg = cfg.get("wake") or {}
        self._is_llamacpp = cfg["model"].get("backend", "llamacpp") == "llamacpp"
        self._endpoint = cfg["model"].get("endpoint", "")

    async def _slot_ready(self) -> bool:
        if not self._is_llamacpp:
            return True                       # stub backend: always up (tests)
        return await ensure_model_up(self._endpoint, self.wake_cfg)

    async def _alert_only(self, signal_id: str, item: dict, revision: int,
                          positions: list[dict], detail: str) -> None:
        """Baseline §11.2 degradation: analyst slot unavailable — journal the
        item + affected positions loudly and surface DEGRADED health. The
        operator acts from the dashboard/notification; the signal is never
        silently dropped."""
        await write_decision(
            signal_id=signal_id, item_id=item["item_id"], item_revision=revision,
            ticker=(positions[0]["ticker"] if positions else None),
            stage="GUARD", agent="A12", action="ALERT_ONLY",
            payload={"positions": [{"position_id": p["position_id"],
                                    "ticker": p["ticker"]} for p in positions],
                     "headline": item.get("headline"),
                     "detail": detail},
            reason=f"analyst slot unavailable — operator attention required: "
                   f"{item.get('headline', '')[:200]}")
        await set_health(HEALTH_COMPONENT, "DEGRADED",
                         f"alert-only: {detail}"[:200])
        log.error("guard ALERT_ONLY", extra=kv(
            signal_id=signal_id, positions=len(positions), detail=detail))

    async def handle(self, msg) -> None:
        body = msg.payload.get("body") or {}
        item_ref = body.get("item_ref") or {}
        routing = body.get("routing") or {}
        item_id = item_ref.get("item_id")
        revision = int(item_ref.get("revision") or 1)
        signal_id = (msg.payload.get("envelope", {}).get("trace", {})
                     .get("signal_id") or item_id)
        if not item_id:
            raise ValueError(f"malformed guard signal ({msg.dedup_key})")

        item = await fetch_item(item_id, revision)
        if item is None:
            raise ValueError(f"item not found in news store: {item_id} rev {revision}")

        # 1. Staleness gate — no tokens for old news (also drains the
        #    pre-Phase-5 backlog as journaled EXPIRED rows).
        age_min = (utcnow() - parse_ts(item["received_ts"])).total_seconds() / 60.0
        if age_min > self.max_age_minutes:
            if not await _existing_guard_decision(signal_id, revision, None):
                await write_decision(
                    signal_id=signal_id, item_id=item_id, item_revision=revision,
                    stage="GUARD", agent="A12", action="EXPIRED",
                    payload={"age_minutes": round(age_min, 1),
                             "max_age_minutes": self.max_age_minutes,
                             "position_ids_routed": routing.get("position_ids", [])},
                    reason=f"guard signal {round(age_min)}min old "
                           f"(max {round(self.max_age_minutes)}) — not evaluated")
            log.info("guard signal expired", extra=kv(
                item_id=item_id, age_min=round(age_min)))
            return

        # 2. Re-resolve positions — the router computed them at triage time.
        positions = await open_positions(list(routing.get("position_ids") or []))
        if not positions:
            if not await _existing_guard_decision(signal_id, revision, None):
                await write_decision(
                    signal_id=signal_id, item_id=item_id, item_revision=revision,
                    stage="GUARD", agent="A12", action="NO_POSITION",
                    payload={"position_ids_routed": routing.get("position_ids", [])},
                    reason="no routed position still open at evaluation time")
            log.info("guard signal without open position", extra=kv(item_id=item_id))
            return

        # 3. Analyst slot availability (probe + wake, then degrade loudly).
        if not await self._slot_ready():
            if not await _existing_guard_decision(signal_id, revision, None):
                await self._alert_only(signal_id, item, revision, positions,
                                       "analyst server down after wake attempt")
            return

        # 4. One verdict per open position.
        schema = guard_json_schema()
        for pos in positions:
            if await _existing_guard_decision(signal_id, revision,
                                              pos["position_id"]):
                continue                              # redelivery no-op
            thesis = await fetch_thesis(pos["thesis_decision_id"])
            context = await build_guard_context(self.md, item, pos)
            pos_pack = position_pack(pos)

            error: GuardValidationError | None = None
            total_latency = 0
            verdict = None
            reply = None
            for _attempt in range(1 + self.retries):
                messages = build_messages(item, pos_pack, thesis, context,
                                          retry_error=error.detail if error else None)
                try:
                    reply = await self.backend.complete(messages, schema)
                except httpx.TransportError as e:
                    # server died mid-conversation: degrade per §11.2
                    await self._alert_only(signal_id, item, revision, [pos],
                                           f"model call failed: {repr(e)[:120]}")
                    verdict = None
                    error = None
                    break
                total_latency += reply.latency_ms
                try:
                    v = validate_guard(reply.text)
                    # code-side cross-field rule: a broken thesis cannot
                    # recommend "hold" (models propose, code disposes)
                    if not v.thesis_intact and v.recommended_action == "hold":
                        raise GuardValidationError(
                            "thesis_intact=false requires recommended_action "
                            "tighten_stop or exit", reply.text)
                    verdict = v
                    break
                except GuardValidationError as e:
                    error = e
                    log.warning("invalid guard output", extra=kv(
                        attempt=_attempt + 1, detail=e.detail[:150]))

            if verdict is None:
                if error is not None:                 # exhausted retries
                    await write_decision(
                        signal_id=signal_id, item_id=item_id,
                        item_revision=revision, ticker=pos["ticker"],
                        stage="GUARD", agent="A12", action="REJECT",
                        payload={"position_id": pos["position_id"],
                                 "raw_output": error.raw, "error": error.detail,
                                 "attempts": 1 + self.retries},
                        reason=f"model output invalid after "
                               f"{1 + self.retries} attempts",
                        model_id=reply.model_id if reply else None,
                        latency_ms=total_latency)
                    log.warning("guard REJECT journaled", extra=kv(
                        item_id=item_id, position_id=pos["position_id"]))
                continue

            pool = await get_pool()
            async with pool.connection() as conn:
                async with conn.transaction():
                    decision_id = await write_decision(
                        signal_id=signal_id, item_id=item_id,
                        item_revision=revision, ticker=pos["ticker"],
                        stage="GUARD", agent="A12",
                        action=ACTION_MAP[verdict.recommended_action],
                        payload={"position_id": pos["position_id"],
                                 "verdict": verdict.model_dump(),
                                 "position": pos_pack,
                                 "context": context},
                        reason=verdict.reason, confidence=verdict.confidence,
                        model_id=self.backend.model_id,
                        latency_ms=total_latency, conn=conn)
                    await write_ledger(conn, decision_id, pos["position_id"],
                                       item_id, verdict)

            log.info("guard verdict", extra=kv(
                signal_id=signal_id, position_id=pos["position_id"],
                ticker=pos["ticker"], intact=verdict.thesis_intact,
                action=verdict.recommended_action, urgency=verdict.urgency,
                watch_hits=len(verdict.watch_hits), latency_ms=total_latency))


async def consume_loop(svc: A12Service, stop: asyncio.Event) -> None:
    hb_detail = f"consuming {IN_QUEUE}"
    await set_health(HEALTH_COMPONENT, "OK", hb_detail)
    last_hb = time.monotonic()
    while not stop.is_set():
        if time.monotonic() - last_hb >= 60.0:
            await set_health(HEALTH_COMPONENT, "OK", hb_detail)
            last_hb = time.monotonic()
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
            log.error("message failed", extra=kv(msg_id=msg.msg_id,
                                                 error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def main() -> None:
    from common.db import close_pool
    cfg = load_yaml(config_path("a12.yaml"))
    await register_config_version("a12 guard service startup")
    svc = A12Service(cfg)
    log.info("A12 up", extra=kv(backend=cfg["model"].get("backend"),
                                model=svc.backend.model_id, consumer=CONSUMER,
                                max_age_minutes=svc.max_age_minutes))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await consume_loop(svc, stop)
    await set_health(HEALTH_COMPONENT, "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
