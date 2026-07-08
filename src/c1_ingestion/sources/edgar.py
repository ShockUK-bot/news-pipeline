"""SEC EDGAR current-events poller.

Fair-access compliance:
  * mandatory User-Agent "<EDGAR_APP_NAME> <EDGAR_CONTACT>" — fails fast at
    startup if EDGAR_CONTACT is unset (better than silent 403s at 2 AM)
  * poll_interval_secs from sources.yaml (default 15s, far under the
    10 req/s cap), one feed request per interval per feed
  * honors 429/503 with a longer sleep

Dedup across polls is inherent: item_id is the accession number, and
store_item() treats an unchanged content_hash as a no-op echo. Amended
filings (8-K/A etc.) have new accession numbers -> new items, which is
correct: an amendment is a new filing event, not a revision of the old text.
"""
from __future__ import annotations

import asyncio
import os

import feedparser
import httpx

from common.log import get_logger, kv
from c1_ingestion.heartbeat import GapMonitor, set_health
from c1_ingestion.normalize import NormalizeError, normalize_edgar
from c1_ingestion.store import quarantine, store_item

log = get_logger("c1.edgar")

COMPONENT = "ingestion:edgar"


def user_agent() -> str:
    contact = os.environ.get("EDGAR_CONTACT")
    if not contact:
        raise RuntimeError("EDGAR_CONTACT not set — SEC fair-access policy requires "
                           "a contact email in the User-Agent (see .env.example)")
    app = os.environ.get("EDGAR_APP_NAME", "Trading System")
    return f"{app} {contact}"


class EdgarSource:
    def __init__(self, cfg: dict, monitor: GapMonitor):
        self.tier = int(cfg.get("tier", 1))
        self.interval = float(cfg.get("poll_interval_secs", 15))
        self.feeds = [{"name": "8-K-current", "url": cfg["feed_url"]}]
        self.feeds += list(cfg.get("extra_feeds", []))
        self.monitor = monitor
        self.ua = user_agent()

    async def run(self) -> None:
        async with httpx.AsyncClient(
            headers={"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"},
            timeout=20.0, follow_redirects=True,
        ) as client:
            await set_health(COMPONENT, "OK", f"polling every {self.interval}s")
            while True:
                for feed in self.feeds:
                    try:
                        await self._poll(client, feed)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.error("poll failed", extra=kv(feed=feed["name"], error=repr(e)[:200]))
                        await set_health(COMPONENT, "DEGRADED", f"{feed['name']}: {e!r}"[:200])
                    await asyncio.sleep(1.0)      # spacing between feeds within a cycle
                await asyncio.sleep(self.interval)

    async def _poll(self, client: httpx.AsyncClient, feed: dict) -> None:
        resp = await client.get(feed["url"])
        if resp.status_code in (429, 503):
            log.warning("rate limited", extra=kv(feed=feed["name"], status=resp.status_code))
            await asyncio.sleep(60)
            return
        resp.raise_for_status()

        parsed = feedparser.parse(resp.text)
        if parsed.bozo and not parsed.entries:
            await quarantine(NormalizeError("UNPARSEABLE_JSON",
                                            f"atom parse: {parsed.bozo_exception!r}",
                                            raw_text=resp.text[:2000]), "edgar")
            return

        stored = 0
        for entry in parsed.entries:
            try:
                item = normalize_edgar(dict(entry), tier=self.tier)
                result = await store_item(item)
                if result.stored:
                    stored += 1
                    self.monitor.mark_activity()
            except NormalizeError as e:
                await quarantine(e, "edgar")
        if stored:
            log.info("poll stored", extra=kv(feed=feed["name"], new=stored))
        # a successful poll is liveness even with zero new filings
        self.monitor.mark_activity()
