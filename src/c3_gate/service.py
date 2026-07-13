"""C3 Market Confirmation Gate service (Phase 3, observe-only downstream —
signal.risk accumulates until Phase 4's A3).

Per message on signal.gate (§7):
  1. compute MarketState from market data + the news store cluster tables
  2. rules.evaluate() -> PASS | VETO
  3. VETO: journal GATE decision with veto_reason; STOP (no message — §8)
     PASS: ONE TRANSACTION: GATE decision + GatePass (§8) on signal.risk,
     snapshot included (A3's limit-pricing basis, copied to intents later)
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal
from datetime import datetime, timedelta, timezone

from common.clock import parse_ts, utcnow
from common.config import config_path, load_yaml
from common.contracts import envelope
from common.db import close_pool, get_pool
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from common.marketdata import adv20, atr14, avg_minute_volume, get_marketdata
from common.queue import ack, claim, enqueue, fail, wait_for_message
from c1_ingestion.heartbeat import set_health
from router.facts import market_open_now, _schedule_cache

from .rules import GateVerdict, MarketState, evaluate

log = get_logger("c3.service")

IN_QUEUE = "signal.gate"
OUT_QUEUE = "signal.risk"
CONSUMER = f"c3-{os.getpid()}"
CONTRACT_GATEPASS = "signal.risk/1"


def _session_window(ts: datetime) -> tuple[datetime, datetime] | None:
    """NYSE session bounds for ts's date (uses the router's cached calendar)."""
    import pandas_market_calendars as mcal
    day_key = ts.strftime("%Y-%m-%d")
    if day_key not in _schedule_cache:
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=day_key, end_date=day_key)
        _schedule_cache[day_key] = None if sched.empty else (
            sched.iloc[0]["market_open"].to_pydatetime(),
            sched.iloc[0]["market_close"].to_pydatetime())
    return _schedule_cache[day_key]


async def _corroboration(item_id: str) -> tuple[int, int]:
    """(independent_outlets, tier_min) for the item's cluster."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT cm.cluster_id FROM news.cluster_members cm
               WHERE cm.item_id = %s LIMIT 1""", (item_id,))
        row = await cur.fetchone()
        if row is None:
            return 1, 3
        cluster_id = row[0]
        cur = await conn.execute(
            """SELECT c.independent_outlets, min(ni.source_tier)
               FROM news.cluster_corroboration c
               JOIN news.cluster_members cm ON cm.cluster_id = c.cluster_id
               JOIN news.news_items ni ON ni.item_id = cm.item_id
                                       AND ni.revision = cm.revision
               WHERE c.cluster_id = %s
               GROUP BY c.independent_outlets""", (cluster_id,))
        row = await cur.fetchone()
        return (row[0], row[1]) if row else (1, 3)


class C3Service:
    def __init__(self, cfg: dict, md=None, now_fn=None):
        self.cfg = cfg["gate"]
        self.md = md or get_marketdata()
        self.now_fn = now_fn or utcnow

    async def build_state(self, thesis: dict, item_id: str,
                          published_ts: datetime, now: datetime) -> MarketState:
        ticker = thesis["ticker"]
        quote = await self.md.snapshot(ticker)
        prev = await self.md.prev_close(ticker)

        pre_bars = await self.md.minute_bars(
            ticker, published_ts - timedelta(minutes=30), published_ts)
        prenews = pre_bars[-1]["close"] if pre_bars else prev

        since = await self.md.minute_bars(ticker, published_ts, now)
        baseline = await self.md.minute_bars(
            ticker, published_ts - timedelta(days=5), published_ts)
        b_vol, s_vol = avg_minute_volume(baseline), avg_minute_volume(since)
        vol_mult = round(s_vol / b_vol, 2) if (s_vol and b_vol) else None

        pub_session = _session_window(published_ts)
        news_in_session = bool(pub_session and
                               pub_session[0] <= published_ts < pub_session[1])
        today_session = _session_window(now)
        minutes_since_open = None
        if today_session and now >= today_session[0]:
            minutes_since_open = int((now - today_session[0]).total_seconds() // 60)

        gap_pct = None
        if not news_in_session:
            day_bars = await self.md.minute_bars(
                ticker, today_session[0], now) if today_session else []
            if day_bars and prev:
                gap_pct = round((day_bars[0]["open"] - prev) / prev, 5)

        outlets, tier_min = await _corroboration(item_id)
        return MarketState(
            prenews_price=prenews, last_price=quote.price, vol_mult=vol_mult,
            minutes_since_publish=int((now - published_ts).total_seconds() // 60),
            news_in_session=news_in_session,
            minutes_since_open=minutes_since_open, gap_pct=gap_pct,
            corroboration_outlets=outlets, tier_min=tier_min)

    async def handle(self, msg) -> None:
        body = msg.payload.get("body") or {}
        thesis = body.get("thesis") or {}
        item_ref = body.get("item_ref") or {}
        item_id = item_ref.get("item_id")
        revision = int(item_ref.get("revision") or 1)
        signal_id = (msg.payload.get("envelope", {}).get("trace", {})
                     .get("signal_id") or item_id)
        if not item_id or not thesis.get("ticker"):
            raise ValueError(f"malformed thesis message ({msg.dedup_key})")

        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT published_ts FROM news.news_items WHERE item_id=%s AND revision=%s",
                (item_id, revision))
            row = await cur.fetchone()
        if row is None:
            raise ValueError(f"item not in news store: {item_id} rev {revision}")
        published_ts = row[0]

        now = self.now_fn()
        state = await self.build_state(thesis, item_id, published_ts, now)
        verdict = evaluate(thesis, state, self.cfg)

        if verdict.verdict == "VETO":
            await write_decision(
                signal_id=signal_id, item_id=item_id, item_revision=revision,
                ticker=thesis["ticker"], stage="GATE", agent="C3",
                action="VETO", veto_reason=verdict.veto_reason,
                payload={"rule": verdict.rule, **(verdict.numbers or {})},
                reason=f"{verdict.veto_reason} ({verdict.rule})",
                regime_id=body.get("regime_id"))
            log.info("gate VETO", extra=kv(signal_id=signal_id,
                                           reason=verdict.veto_reason,
                                           rule=verdict.rule))
            return

        quote = await self.md.snapshot(thesis["ticker"])
        daily = await self.md.daily_bars(thesis["ticker"], 30)
        snapshot = {"ref_price": quote.price, "bid": quote.bid, "ask": quote.ask,
                    "spread_bps": quote.spread_bps, "adv_20d": adv20(daily),
                    "atr_14": atr14(daily), "ts": quote.ts.isoformat()}
        gate_body = {"thesis": thesis,
                     "gate": {"verdict": "PASS", "rule": verdict.rule,
                              **(verdict.numbers or {}), "snapshot": snapshot}}

        async with pool.connection() as conn:
            async with conn.transaction():
                decision_id = await write_decision(
                    signal_id=signal_id, item_id=item_id, item_revision=revision,
                    ticker=thesis["ticker"], stage="GATE", agent="C3",
                    action="PASS",
                    payload=gate_body["gate"],
                    reason=f"confirmed ({verdict.rule})",
                    regime_id=body.get("regime_id"), conn=conn)
                out = envelope(CONTRACT_GATEPASS, "C3", signal_id, item_id,
                               revision, gate_body)
                out["envelope"]["trace"]["decision_id"] = decision_id
                await enqueue(OUT_QUEUE, f"{signal_id}:{revision}", out, conn=conn)

        log.info("gate PASS", extra=kv(signal_id=signal_id,
                                       ticker=thesis["ticker"], rule=verdict.rule))


async def consume_loop(svc: C3Service, stop: asyncio.Event) -> None:
    import time
    hb_detail = f"consuming {IN_QUEUE}"
    await set_health("gate", "OK", hb_detail)
    last_hb = time.monotonic()
    while not stop.is_set():
        if time.monotonic() - last_hb >= 60.0:
            await set_health("gate", "OK", hb_detail)
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
            log.error("message failed", extra=kv(msg_id=msg.msg_id, error=repr(e)[:300]))
            await fail(msg.msg_id, repr(e))


async def main() -> None:
    cfg = load_yaml(config_path("gate.yaml"))
    await register_config_version("c3 gate service startup")
    svc = C3Service(cfg)
    log.info("C3 up", extra=kv(consumer=CONSUMER))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await consume_loop(svc, stop)
    await set_health("gate", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

