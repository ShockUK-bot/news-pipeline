"""C1 ingestion service. One process, asyncio: one task per enabled source
plus a watchdog task that ticks every GapMonitor and refreshes the top-level
journal.health row. Sources that crash are restarted by their own run() loops
(Alpaca) or supervised here (pollers restart with the supervisor's backoff).

Run: python -m c1_ingestion.service            (with src/ on PYTHONPATH)
Config: config/sources.yaml, env per .env.example.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys

from common.db import close_pool
from common.log import get_logger, kv
from c1_ingestion.heartbeat import GapMonitor, set_health
from c1_ingestion.sources.alpaca_ws import AlpacaNewsSource
from c1_ingestion.sources.edgar import EdgarSource
from c1_ingestion.sources.rss import RssSource

log = get_logger("c1.service")

WATCHDOG_INTERVAL = 30.0


def load_sources_config() -> dict:
    from common.config import load_yaml, config_path
    return load_yaml(config_path("sources.yaml"))


async def _supervised(name: str, coro_factory, restart_delay: float = 5.0) -> None:
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("source task crashed; restarting",
                      extra=kv(source=name, error=repr(e)[:300], delay=restart_delay))
            await asyncio.sleep(restart_delay)


async def _watchdog(monitors: list[GapMonitor]) -> None:
    while True:
        for m in monitors:
            try:
                await m.check()
            except Exception as e:
                log.error("gap check failed", extra=kv(source=m.source, error=repr(e)[:200]))
        await set_health("ingestion", "OK", f"{len(monitors)} sources monitored")
        await asyncio.sleep(WATCHDOG_INTERVAL)


async def main() -> None:
    cfg = load_sources_config()
    tasks: list[asyncio.Task] = []
    monitors: list[GapMonitor] = []

    if cfg.get("alpaca", {}).get("enabled"):
        c = cfg["alpaca"]
        mon = GapMonitor("alpaca_benzinga", c["gap_threshold_market_secs"],
                         c["gap_threshold_offhours_secs"])
        monitors.append(mon)
        src = AlpacaNewsSource(c, mon)
        tasks.append(asyncio.create_task(_supervised("alpaca", src.run)))

    if cfg.get("edgar", {}).get("enabled"):
        c = cfg["edgar"]
        mon = GapMonitor("edgar", c["gap_threshold_market_secs"],
                         c["gap_threshold_offhours_secs"])
        monitors.append(mon)
        src = EdgarSource(c, mon)
        tasks.append(asyncio.create_task(_supervised("edgar", src.run)))

    if cfg.get("rss", {}).get("enabled"):
        c = cfg["rss"]
        mon = GapMonitor("rss", c["gap_threshold_market_secs"],
                         c["gap_threshold_offhours_secs"])
        monitors.append(mon)
        src = RssSource(c, mon)
        tasks.append(asyncio.create_task(_supervised("rss", src.run)))

    if not tasks:
        log.error("no sources enabled in sources.yaml")
        sys.exit(1)

    tasks.append(asyncio.create_task(_watchdog(monitors)))
    log.info("C1 up", extra=kv(sources=len(monitors)))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await set_health("ingestion", "DOWN", "clean shutdown")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

