"""C12 burst stream service (v0.15.0). Research-first: journals bursts and their
forward paths, places no orders, enqueues nothing.

Run: python -m c12_burst.service (systemd: c12-burst.service).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import signal as _signal
import time
from datetime import datetime, timezone
from typing import Optional

import websockets

from common.clock import utcnow
from common.config import config_path, load_yaml
from common.db import get_pool, close_pool
from common.journal import register_config_version
from common.log import get_logger, kv
from c1_ingestion.heartbeat import Heartbeat, set_health

from .book import SymbolBook
from .detect import CT, Event, evaluate, in_session

log = get_logger("c12.burst")
COMPONENT = "burst"


# ---------------------------------------------------------------------------
# universe
# ---------------------------------------------------------------------------

async def journal_universe(min_adv: float, days: int) -> list[str]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """WITH u AS (
                 SELECT ticker, max((metrics->>'adv20_dollars')::numeric) AS adv
                 FROM journal.scanner_candidates
                 WHERE ts >= now() - make_interval(days => %s) AND metrics ? 'adv20_dollars'
                 GROUP BY 1
                 UNION ALL
                 SELECT ticker, max((payload->'snapshot'->>'adv_20d')::numeric
                                    * (payload->'snapshot'->>'ref_price')::numeric)
                 FROM journal.decisions
                 WHERE stage='GATE' AND payload ? 'snapshot' AND ticker IS NOT NULL
                   AND ts >= now() - make_interval(days => %s)
                 GROUP BY 1)
               SELECT ticker FROM u GROUP BY 1 HAVING max(adv) >= %s ORDER BY 1""",
            (days, days, min_adv))
        return [r[0] for r in await cur.fetchall()]


async def validate_symbols(symbols: list[str]) -> list[str]:
    """Drop anything Alpaca does not know or will not trade (a bad symbol in a
    subscribe message can reject the whole request)."""
    from common.assets import AssetsClient
    client = AssetsClient(ttl_secs=3600)
    ok = []
    for s in symbols:
        try:
            a = await client.get(s)
            if a.tradable:
                ok.append(s)
        except Exception as e:                                # noqa: BLE001
            log.warning("symbol dropped", extra=kv(symbol=s, error=repr(e)[:80]))
    return ok


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

class C12Service:
    def __init__(self, cfg: dict, universe: list[str], now_fn=None):
        self.cfg = cfg
        self.det = cfg.get("detect") or {}
        self.universe = universe
        self.books: dict[str, SymbolBook] = {
            s: SymbolBook(s, int(cfg.get("keep_secs", 2700))) for s in universe}
        self.last_fire: dict[str, float] = {}
        self.pending: list[dict] = []          # events awaiting forward fill
        self.now_fn = now_fn or time.time
        self.stats = {"trades": 0, "bars": 0, "events": 0, "frames": 0}
        self.key = os.environ.get("ALPACA_KEY_ID")
        self.secret = os.environ.get("ALPACA_SECRET_KEY")
        if not self.key or not self.secret:
            raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set")
        self.feed = (os.environ.get("ALPACA_FEED") or cfg.get("feed") or "sip").strip().lower()

    # ---- stream ------------------------------------------------------------------
    async def run_stream(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                await self._session(stop)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:                            # noqa: BLE001
                msg = repr(e)[:200]
                log.error("stream session failed", extra=kv(error=msg))
                await set_health(COMPONENT, "DEGRADED", f"reconnecting: {msg}")
                if "subscription" in msg.lower() and self.feed == "sip":
                    log.warning("SIP refused; falling back to IEX feed")
                    self.feed = "iex"
            await asyncio.sleep(min(backoff, 60) * (0.5 + random.random()))
            backoff = min(backoff * 2, 60)

    async def _session(self, stop: asyncio.Event) -> None:
        url = f"wss://stream.data.alpaca.markets/v2/{self.feed}"
        async with websockets.connect(url, max_size=2**24, ping_interval=20) as ws:
            await self._expect(ws, "connected")
            await ws.send(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
            await self._expect(ws, "authenticated")
            await ws.send(json.dumps({"action": "subscribe", "trades": self.universe,
                                      "bars": self.universe}))
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            frames = json.loads(raw)
            sub = next((f for f in (frames if isinstance(frames, list) else [frames])
                        if f.get("T") == "subscription"), None)
            if sub is None:
                raise RuntimeError(f"no subscription ack: {str(frames)[:200]}")
            n = len(sub.get("trades") or [])
            log.info("subscribed", extra=kv(feed=self.feed, symbols=n))
            await set_health(COMPONENT, "OK", f"{self.feed} stream, {n} symbols")
            async for raw in ws:
                if stop.is_set():
                    return
                self._handle(raw)

    async def _expect(self, ws, msg: str) -> None:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        frames = json.loads(raw)
        for f in frames if isinstance(frames, list) else [frames]:
            if f.get("T") == "success" and f.get("msg") == msg:
                return
            if f.get("T") == "error":
                raise RuntimeError(f"alpaca error frame: {f}")
        raise RuntimeError(f"expected {msg!r}, got: {str(frames)[:200]}")

    def _handle(self, raw) -> None:
        try:
            frames = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        self.stats["frames"] += 1
        for f in frames if isinstance(frames, list) else [frames]:
            t = f.get("T")
            if t == "t":
                b = self.books.get(f.get("S"))
                if b is None:
                    continue
                ts = _parse_ts(f.get("t"))
                if ts is None:
                    continue
                b.add_trade(ts, float(f["p"]), float(f.get("s") or 0))
                self.stats["trades"] += 1
            elif t == "b":
                self.stats["bars"] += 1
            elif t == "error":
                log.error("stream error frame", extra=kv(frame=str(f)[:200]))

    # ---- detection ---------------------------------------------------------------
    async def run_detect(self, stop: asyncio.Event) -> None:
        hb = Heartbeat(COMPONENT, "stream idle")
        await hb.start()
        interval = float(self.cfg.get("detect_interval_secs", 5))
        last_stats = 0.0
        while not stop.is_set():
            now = self.now_fn()
            try:
                if in_session(now, self.det):
                    for book in self.books.values():
                        prior = self.last_fire.get(book.symbol)
                        ev = evaluate(book, now, self.det, self.last_fire)
                        if ev is not None:
                            # v0.15.4: a second burst in the same name inside
                            # repeat_window_secs is a different population
                            # (it lost in both measured sessions); flag it so
                            # the report can keep it out of the fade sample.
                            gap = (now - prior) if prior is not None else None
                            ev.detail["prior_burst_secs"] = (round(gap) if gap is not None else None)
                            ev.detail["repeat"] = bool(
                                gap is not None and gap <= float(self.det.get("repeat_window_secs", 1800)))
                            size = max(abs(ev.ret_60s or 0.0), abs(ev.ret_120s or 0.0))
                            ev.detail["fade_oversize"] = bool(
                                size > float(self.det.get("fade_max_burst_pct", 0.015)))
                            await self._journal_event(ev)
                    await self._fill_pending(now)
                    await hb.tick(f"{self.feed} stream, {len(self.books)} symbols, "
                                  f"{self.stats['trades']} trades, {self.stats['events']} events today")
                else:
                    await hb.tick(f"{self.feed} stream idle (off session), "
                                  f"{self.stats['trades']} trades since start")
                if now - last_stats >= 600:
                    log.info("stats", extra=kv(**self.stats, pending=len(self.pending)))
                    last_stats = now
            except Exception as e:                            # noqa: BLE001
                log.error("detect pass error", extra=kv(error=repr(e)[:300]))
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _context(self, symbol: str, now: float) -> tuple[bool, Optional[str], bool]:
        """(news_anchored, news_direction, scanner_known) from the journal."""
        pool = await get_pool()
        since = datetime.fromtimestamp(now - float(self.det.get("news_window_secs", 900)), timezone.utc)
        today = datetime.fromtimestamp(now, CT).date()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT payload->'triage'->>'direction_hint' FROM journal.decisions
                   WHERE stage='TRIAGE' AND action='ESCALATE' AND ticker=%s AND ts >= %s
                   ORDER BY ts DESC LIMIT 1""", (symbol, since))
            row = await cur.fetchone()
            cur = await conn.execute(
                """SELECT 1 FROM journal.scanner_candidates
                   WHERE ticker=%s AND scan_date=%s LIMIT 1""", (symbol, today))
            sc = await cur.fetchone()
        return (row is not None, row[0] if row else None, sc is not None)

    async def _spread_bps(self, symbol: str) -> Optional[float]:
        try:
            from common.marketdata import AlpacaData
            q = await AlpacaData().snapshot(symbol)
            return float(q.spread_bps) if q.spread_bps is not None else None
        except Exception:                                     # noqa: BLE001
            return None

    async def _journal_event(self, ev: Event) -> None:
        news, news_dir, scanner_known = await self._context(ev.symbol, ev.ts)
        spread = await self._spread_bps(ev.symbol)
        rows = [("momentum", ev.direction), ("fade", -ev.direction)]
        if news:
            rows.append(("news_anchored", ev.direction))
        pool = await get_pool()
        ids = []
        async with pool.connection() as conn:
            async with conn.transaction():
                for rule, direction in rows:
                    cur = await conn.execute(
                        """INSERT INTO journal.burst_events
                           (symbol, ts, rule, direction, price, ret_60s, ret_120s, vol_mult,
                            spread_bps, new_extreme, news_anchored, news_direction, scanner_known,
                            feed, detail)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           RETURNING event_id""",
                        (ev.symbol, datetime.fromtimestamp(ev.ts, timezone.utc), rule,
                         "up" if direction > 0 else "down", ev.price, ev.ret_60s, ev.ret_120s,
                         ev.vol_mult, spread, ev.new_extreme, news, news_dir, scanner_known,
                         self.feed, json.dumps(ev.detail)))
                    ids.append(((await cur.fetchone())[0], direction))
        for event_id, direction in ids:
            self.pending.append({"event_id": event_id, "symbol": ev.symbol, "ts": ev.ts,
                                 "px": ev.price, "direction": direction,
                                 "spread_bps": spread})
        self.stats["events"] += 1
        log.info("burst", extra=kv(symbol=ev.symbol, dir=ev.direction,
                                   ret60=round(ev.ret_60s or 0, 4), ret120=round(ev.ret_120s or 0, 4),
                                   vol_mult=round(ev.vol_mult or 0, 1), spread_bps=spread,
                                   news=news, scanner=scanner_known))

    def _realistic(self, book, p: dict, horizon: int, bracket_keys) -> Optional[dict]:
        """Score p's path from the first print entry_delay_secs after detection,
        with half the quoted spread as crossing cost (long pays up, short sells
        down). Returns None when no print exists after the delay."""
        delay = float(self.det.get("entry_delay_secs", 30))
        px, ts = book.entry_after(p["ts"], delay)
        if px is None or ts is None:
            return None
        half = (float(p.get("spread_bps") or 0.0) / 2.0) / 10000.0
        entry = px * (1 + p["direction"] * half)
        rem = max(int(horizon - (ts - p["ts"])), 60)
        rp = book.path(ts, entry, p["direction"], rem,
                       float(self.det.get("score_target", 0.01)),
                       float(self.det.get("score_stop", 0.007)))
        p30 = book.price_at(p["ts"] + horizon)
        ret_30 = ((p30 / entry - 1) if p["direction"] > 0 else (1 - p30 / entry)) if p30 else None
        out = {"delay_secs": delay, "entry_px": round(entry, 4), "print_px": px,
               "entry_ts": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
               "half_spread_bps": round(half * 10000, 2),
               "ret_30m": (round(ret_30, 5) if ret_30 is not None else None),
               "max_fav_pct": round(rp["max_fav_pct"], 5), "max_adv_pct": round(rp["max_adv_pct"], 5),
               "first_hit": rp["first_hit"], "first_hit_min": rp["first_hit_min"],
               "brackets": {}}
        for key in bracket_keys:
            t = float(key.split("_")[0][1:]) / 100; st = float(key.split("_")[1][1:]) / 100
            bp = book.path(ts, entry, p["direction"], rem, t, st)
            out["brackets"][key] = [bp["first_hit"], bp["first_hit_min"]]
        return out

    async def _fill_pending(self, now: float) -> None:
        horizon = float(self.det.get("horizon_secs", 1800))
        done = []
        for p in self.pending:
            if now - p["ts"] < horizon + 10:
                continue
            book = self.books.get(p["symbol"])
            if book is None:
                done.append(p)
                continue
            path = book.path(p["ts"], p["px"], p["direction"], int(horizon),
                             float(self.det.get("score_target", 0.01)),
                             float(self.det.get("score_stop", 0.007)))
            # v0.15.3: the same path against several target/stop brackets, so
            # "what size of move is realistic" is measured, not guessed.
            # Stored in detail.brackets as {"t0.5_s0.5": ["target", 3.2], ...}
            brackets = {}
            for t, s in (self.det.get("score_brackets")
                         or [[0.005, 0.005], [0.007, 0.005], [0.01, 0.007],
                             [0.01, 0.01], [0.005, 0.0035]]):
                bp = book.path(p["ts"], p["px"], p["direction"], int(horizon),
                               float(t), float(s))
                brackets[f"t{float(t)*100:g}_s{float(s)*100:g}"] = [
                    bp["first_hit"], bp["first_hit_min"]]
            # v0.15.4: the same scoring from a REALISTIC entry. Session 1 and 2
            # showed most fade "wins" land inside the first minute, i.e. the
            # detection print is the spike itself. An order sent at detection
            # fills at the first print after entry_delay_secs and crosses
            # half the spread; score that path too so the two can be compared.
            realistic = self._realistic(book, p, int(horizon), brackets.keys())
            pool = await get_pool()
            async with pool.connection() as conn:
                await conn.execute(
                    """UPDATE journal.burst_events
                       SET p_1m=%s, p_5m=%s, p_15m=%s, p_30m=%s, max_fav_pct=%s, max_adv_pct=%s,
                           first_hit=%s, first_hit_min=%s, complete=true,
                           detail = detail || %s::jsonb
                       WHERE event_id=%s""",
                    (path["p_1m"], path["p_5m"], path["p_15m"], path["p_30m"],
                     round(path["max_fav_pct"], 5), round(path["max_adv_pct"], 5),
                     path["first_hit"], path["first_hit_min"],
                     json.dumps({"brackets": brackets, "realistic": realistic}), p["event_id"]))
            done.append(p)
        for p in done:
            self.pending.remove(p)


def _parse_ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        # RFC3339 with nanoseconds: trim to microseconds for fromisoformat
        if "." in s:
            head, tail = s.split(".", 1)
            frac = tail.rstrip("Z")[:6]
            s = f"{head}.{frac}+00:00"
        else:
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


async def main() -> None:
    cfg = load_yaml(config_path("burst.yaml"))
    await register_config_version("c12 burst service startup")
    ucfg = cfg.get("universe") or {}
    # str() guards YAML surprises (a bare `ON` parses as boolean true)
    syms = set(str(s).upper() for s in (ucfg.get("static") or []) if isinstance(s, str))
    if ucfg.get("from_journal", True):
        syms.update(await journal_universe(float(ucfg.get("min_adv_dollars", 1e9)),
                                           int(ucfg.get("journal_days", 30))))
    for s in ucfg.get("exclude") or []:
        syms.discard(s.upper())
    universe = await validate_symbols(sorted(syms))
    cap = int(ucfg.get("max_symbols", 400))
    universe = universe[:cap]
    log.info("C12 up", extra=kv(symbols=len(universe), feed=os.environ.get("ALPACA_FEED", "sip")))
    await set_health(COMPONENT, "OK", f"starting, {len(universe)} symbols")
    svc = C12Service(cfg, universe)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    stream = asyncio.create_task(svc.run_stream(stop))
    detect = asyncio.create_task(svc.run_detect(stop))
    await stop.wait()
    stream.cancel()
    await asyncio.gather(stream, detect, return_exceptions=True)
    await set_health(COMPONENT, "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
