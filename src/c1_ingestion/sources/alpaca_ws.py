"""Alpaca news websocket (wss://stream.data.alpaca.markets/v1beta1/news).

Protocol:
  connect -> recv [{"T":"success","msg":"connected"}]
  send    {"action":"auth","key":K,"secret":S}
  recv    [{"T":"success","msg":"authenticated"}]
  send    {"action":"subscribe","news":["*"]}          # wildcard firehose (baseline)
  recv    [{"T":"subscription","news":["*"]}]
  then news frames: [{"T":"n", ...}, ...]

Reconnect: exponential backoff (base..max from sources.yaml) with jitter.
Every parse failure quarantines (UNPARSEABLE_JSON); every unknown frame type
is logged, not dropped silently. websockets' built-in ping/pong (20s default)
handles dead-TCP detection; the GapMonitor handles "connected but silent".
"""
from __future__ import annotations

import asyncio
import json
import os
import random

import websockets

from common.log import get_logger, kv
from c1_ingestion.heartbeat import GapMonitor, set_health
from c1_ingestion.normalize import NormalizeError, normalize_alpaca
from c1_ingestion.store import quarantine, store_item

log = get_logger("c1.alpaca")

COMPONENT = "ingestion:alpaca"


class AlpacaNewsSource:
    def __init__(self, cfg: dict, monitor: GapMonitor):
        self.url = os.environ.get("ALPACA_NEWS_WS", "wss://stream.data.alpaca.markets/v1beta1/news")
        self.key = os.environ.get("ALPACA_KEY_ID")
        self.secret = os.environ.get("ALPACA_SECRET_KEY")
        if not self.key or not self.secret:
            raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set (see .env.example)")
        self.tier = int(cfg.get("tier", 2))
        self.backoff_base = float(cfg.get("reconnect_base_secs", 1))
        self.backoff_max = float(cfg.get("reconnect_max_secs", 60))
        self.monitor = monitor

    async def run(self) -> None:
        backoff = self.backoff_base
        while True:
            try:
                await self._session()
                backoff = self.backoff_base          # clean close -> reset
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("session failed", extra=kv(error=repr(e)[:200]))
                await set_health(COMPONENT, "DEGRADED", f"reconnecting: {e!r}"[:200])
            sleep = min(backoff, self.backoff_max) * (0.5 + random.random())
            await asyncio.sleep(sleep)
            backoff = min(backoff * 2, self.backoff_max)

    async def _session(self) -> None:
        async with websockets.connect(self.url, max_size=2**22) as ws:
            await self._expect(ws, "connected")
            await ws.send(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
            await self._expect(ws, "authenticated")
            await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
            log.info("subscribed to wildcard firehose")
            await set_health(COMPONENT, "OK", "connected, wildcard subscribed")

            async for frame in ws:
                await self._handle_frame(frame)

    async def _expect(self, ws, msg: str) -> None:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        frames = json.loads(raw)
        for f in frames if isinstance(frames, list) else [frames]:
            if f.get("T") == "success" and f.get("msg") == msg:
                return
            if f.get("T") == "error":
                raise RuntimeError(f"alpaca error frame: {f}")
        raise RuntimeError(f"expected {msg!r}, got: {str(frames)[:200]}")

    async def _handle_frame(self, raw) -> None:
        try:
            frames = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            await quarantine(NormalizeError("UNPARSEABLE_JSON", str(e), raw_text=str(raw)[:2000]),
                             "alpaca_benzinga")
            return
        for f in frames if isinstance(frames, list) else [frames]:
            t = f.get("T")
            if t == "n":
                self.monitor.mark_activity()
                try:
                    item = normalize_alpaca(f, tier=self.tier)
                    await store_item(item)
                except NormalizeError as e:
                    await quarantine(e, "alpaca_benzinga")
            elif t in ("success", "subscription"):
                continue
            elif t == "error":
                log.error("stream error frame", extra=kv(frame=str(f)[:200]))
            else:
                log.warning("unknown frame type", extra=kv(T=t))
