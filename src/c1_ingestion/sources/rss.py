"""Generic RSS poller (Tier 3). Conditional GET via ETag/Last-Modified where
the feed supports it; per-poll dedup is inherent via item_id + content_hash
(store_item no-ops echoes). Like EDGAR, a successful poll marks liveness —
the gap we track for pollers is "cannot fetch", not "publisher quiet".
"""
from __future__ import annotations

import asyncio

import feedparser
import httpx

from common.log import get_logger, kv
from c1_ingestion.heartbeat import GapMonitor, set_health
from c1_ingestion.normalize import NormalizeError, normalize_rss
from c1_ingestion.store import quarantine, store_item

log = get_logger("c1.rss")

COMPONENT = "ingestion:rss"


class RssSource:
    def __init__(self, cfg: dict, monitor: GapMonitor):
        self.tier = int(cfg.get("tier", 3))
        self.interval = float(cfg.get("poll_interval_secs", 60))
        self.feeds = list(cfg.get("feeds", []))
        self.monitor = monitor
        self._cache: dict[str, dict] = {}     # feed name -> {etag, last_modified}

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                     headers={"User-Agent": "news-pipeline/0.1"}) as client:
            await set_health(COMPONENT, "OK", f"{len(self.feeds)} feeds, every {self.interval}s")
            while True:
                for feed in self.feeds:
                    try:
                        await self._poll(client, feed)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.error("poll failed", extra=kv(feed=feed["name"], error=repr(e)[:200]))
                        await set_health(COMPONENT, "DEGRADED", f"{feed['name']}: {e!r}"[:200])
                    await asyncio.sleep(0.5)
                await asyncio.sleep(self.interval)

    async def _poll(self, client: httpx.AsyncClient, feed: dict) -> None:
        name, url = feed["name"], feed["url"]
        headers = {}
        cache = self._cache.get(name, {})
        if cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        if cache.get("last_modified"):
            headers["If-Modified-Since"] = cache["last_modified"]

        resp = await client.get(url, headers=headers)
        if resp.status_code == 304:
            self.monitor.mark_activity()
            return
        resp.raise_for_status()
        self._cache[name] = {"etag": resp.headers.get("ETag"),
                             "last_modified": resp.headers.get("Last-Modified")}

        parsed = feedparser.parse(resp.text)
        if parsed.bozo and not parsed.entries:
            await quarantine(NormalizeError("UNPARSEABLE_JSON",
                                            f"rss parse: {parsed.bozo_exception!r}",
                                            raw_text=resp.text[:2000]), f"rss:{name}")
            return

        stored = 0
        for entry in parsed.entries:
            try:
                item = normalize_rss(dict(entry), feed_name=name, tier=self.tier)
                result = await store_item(item)
                if result.stored:
                    stored += 1
            except NormalizeError as e:
                await quarantine(e, f"rss:{name}")
        if stored:
            log.info("poll stored", extra=kv(feed=name, new=stored))
        self.monitor.mark_activity()
