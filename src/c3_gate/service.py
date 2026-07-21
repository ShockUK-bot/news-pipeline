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
from typing import Optional

from common.clock import parse_ts, utcnow
from common.config import config_path, load_yaml
from common.contracts import envelope
from common.db import close_pool, get_pool
from common.journal import register_config_version, write_decision
from common.log import get_logger, kv
from common.marketdata import adv20, atr14, avg_minute_volume, get_marketdata
from common.queue import ack, claim, defer, enqueue, fail, wait_for_message
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


class DeferEvaluation(Exception):
    """Raised by handle() when the since-news window cannot yet contain enough
    COMPLETED minute bars to evaluate volume confirmation. The consume loop
    responds by deferring the message (queue.defer) instead of acking or
    failing it. Deferral is scheduling, not an error."""

    def __init__(self, delay_secs: float, mature_ts: datetime):
        self.delay_secs = delay_secs
        self.mature_ts = mature_ts
        super().__init__(f"since-window immature until {mature_ts.isoformat()}")


def bars_mature_ts(published_ts: datetime, min_bars: int) -> datetime:
    """Earliest instant the window [published_ts, now] can hold min_bars
    COMPLETED minute bars.

    Minute bars are stamped on minute boundaries and only exist once their
    minute closes: for news published 10:00:12 the first bar the window can
    contain is stamped 10:01 and completes at 10:02. Evaluating before then
    returns ZERO bars for ANY ticker — which is how fast Alpaca-news items
    (GM, EMBJ, 2026-07-21) were terminally vetoed MARKETDATA_MISSING ~60s
    after publish. Boundary-published news is treated one minute
    conservatively (its own minute's bar is ignored) to keep the arithmetic
    obvious."""
    first_boundary = (published_ts.replace(second=0, microsecond=0)
                      + timedelta(minutes=1))
    return first_boundary + timedelta(minutes=min_bars)


def defer_delay(published_ts: datetime, now: datetime,
                min_bars: int) -> Optional[float]:
    """Seconds to defer before the window is evaluable, or None if mature.
    Floored at 5s (a near-mature message must not busy-loop re-claims) and
    capped at 300s (a grossly future-skewed published_ts must not park a
    message for hours — repeated capped defers cost nothing because defer
    refunds the claim attempt)."""
    mature = bars_mature_ts(published_ts, min_bars)
    if now >= mature:
        return None
    return min(max((mature - now).total_seconds() + 1.0, 5.0), 300.0)


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

        # v0.11.10: in-session news feeds the intraday volume rule, which
        # needs COMPLETED minute bars. A fast item can reach C3 ~60s after
        # publish, when the since-window physically cannot contain one yet —
        # that is missing time, not missing data, so DEFER instead of vetoing
        # MARKETDATA_MISSING. Off-session news takes the open-handoff rule
        # (gap-based, no vol_mult) and is never deferred.
        pub_session = _session_window(published_ts)
        if pub_session and pub_session[0] <= published_ts < pub_session[1]:
            min_bars = int(self.cfg.get("min_confirm_bars", 3))
            delay = defer_delay(published_ts, now, min_bars)
            if delay is not None:
                mature = bars_mature_ts(published_ts, min_bars)
                log.info("gate DEFER", extra=kv(
                    signal_id=signal_id, ticker=thesis["ticker"],
                    delay_secs=round(delay, 1), mature_ts=mature.isoformat()))
                raise DeferEvaluation(delay, mature)

        state = await self.build_state(thesis, item_id, published_ts, now)
        if state.vol_mult is not None:
            # v0.5.9: C3 is the natural marketdata heartbeat during market
            # hours — every successful volume computation refreshes it, so a
            # stale heartbeat now genuinely means data trouble (the old one
            # froze whenever no positions were open and confused deadman).
            await set_health("marketdata", "OK",
                             f"volume bars ok ({thesis['ticker']})")
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
            if verdict.veto_reason == "MARKETDATA_MISSING":
                # v0.5.9: surface data starvation on the dashboard/deadman
                # instead of hiding it inside a normal-looking veto.
                await set_health(
                    "marketdata", "DEGRADED",
                    f"no volume bars for {thesis['ticker']} ({item_id})")
                log.warning("volume bars missing", extra=kv(
                    ticker=thesis["ticker"], item_id=item_id))
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
        # Periodic heartbeat (v0.11.7) — C3 used to write health only at
        # startup, so the dead-man flagged 'stale: gate' forever after 5 min.
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
        except DeferEvaluation as d:
            # v0.11.10: not a failure — release the message back to the queue
            # with a delay; it re-arrives once minute bars can exist.
            await defer(msg.msg_id, d.delay_secs)
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

