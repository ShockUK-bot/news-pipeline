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
    """Minimal YAML loader — sources.yaml uses only maps/lists/scalars.
    Uses PyYAML if present; otherwise a tiny built-in parser sufficient for
    the file we ship (keeps the runtime dependency-light)."""
    path = os.environ.get("SOURCES_CONFIG",
                          os.path.join(os.path.dirname(__file__), "..", "..", "config", "sources.yaml"))
    try:
        import yaml  # type: ignore
        with open(path) as f:
            return yaml.safe_load(f)
    except ImportError:
        return _tiny_yaml(path)


def _tiny_yaml(path: str) -> dict:
    """Parser for the strict subset used by config/*.yaml:
    nested maps by 2-space indent, lists of scalars or single-level maps,
    str/int/float/bool scalars, quotes optional, # comments."""
    root: dict = {}
    stack: list[tuple[int, dict | list]] = [(-1, root)]
    with open(path) as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            stripped = line.split("#", 1)[0].rstrip() if not line.lstrip().startswith("#") else ""
            if not stripped.strip():
                continue
            indent = len(stripped) - len(stripped.lstrip())
            content = stripped.strip()
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1]
            if content.startswith("- "):
                item_src = content[2:].strip()
                if not hasattr(parent, "append"):
                    raise ValueError(f"list item outside list: {raw_line!r}")
                if ":" in item_src:
                    k, v = item_src.split(":", 1)
                    obj = {k.strip(): _scalar(v.strip())}
                    parent.append(obj)
                    stack.append((indent, obj))
                else:
                    parent.append(_scalar(item_src))
            elif content.endswith(":"):
                key = content[:-1].strip()
                # lookahead not available line-by-line; default to dict, swap to
                # list on first "- " child via a placeholder
                node = _LazyNode()
                parent[key] = node
                stack.append((indent, node))
            else:
                k, v = content.split(":", 1)
                if isinstance(parent, _LazyNode):
                    parent.as_dict()
                parent[k.strip()] = _scalar(v.strip())
    return _resolve(root)


class _LazyNode(dict):
    """Starts as dict; converts semantics to list if children are list items."""
    def __init__(self):
        super().__init__()
        self._list: list | None = None

    def append(self, x):
        if self._list is None:
            self._list = []
        self._list.append(x)

    def as_dict(self):
        return self

    def resolved(self):
        return self._list if self._list is not None else dict(self)


def _resolve(node):
    if isinstance(node, _LazyNode):
        node = node.resolved()
    if isinstance(node, dict):
        return {k: _resolve(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v) for v in node]
    return node


def _scalar(s: str):
    s = s.strip().strip('"').strip("'")
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


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
