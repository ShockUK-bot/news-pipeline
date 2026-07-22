"""Generic RSS poller (Tier 3). Conditional GET via ETag/Last-Modified where
the feed supports it; per-poll dedup is inherent via item_id + content_hash
(store_item no-ops echoes). Like EDGAR, a successful poll marks liveness —
the gap we track for pollers is "cannot fetch", not "publisher quiet".

v0.11.1 — two independent fixes for the same symptom (prnewswire-news
returning HTTP 404 on every poll, which painted the whole `ingestion:rss`
dashboard row yellow even though the other two feeds were fine):
  * Default User-Agent changed from the literal "news-pipeline/0.1" to a
    realistic browser string. Several wire services quietly 404/403
    anything that looks like a bot rather than answering honestly — this
    costs nothing to try and needs no config change. Still overridable per
    deployment via `sources.yaml: rss.user_agent` if a specific publisher
    objects to this one too.
  * Health is now tracked per feed (`ingestion:rss:<name>`) in addition to
    the existing aggregate `ingestion:rss` row. The aggregate now only
    flips to DEGRADED once every configured feed is failing at the same
    time — one dead feed among healthy siblings no longer paints the whole
    row yellow, and the dashboard shows exactly which named feed is
    unhappy. The gap-threshold liveness check in heartbeat.GapMonitor is
    unchanged and still owns the authoritative "has this source gone
    properly silent" alerting that feeds the dead-man ladder.

v0.11.11 — the aggregate `ingestion:rss` row now reflects the CURRENT state
every poll cycle instead of only ever being set to DEGRADED. Before this,
`run()` set the aggregate to OK once at startup and to DEGRADED when every
feed happened to fail in the same pass, but never wrote OK again once the
feeds recovered — so a single simultaneous blip (e.g. a brief upstream/ISP
hiccup that momentarily hit all feeds at once) latched the row red until the
service was restarted, even though the per-feed rows had long since gone
green. Two changes: (1) the aggregate is recomputed and rewritten each
cycle, so recovery clears it automatically; (2) it only flips to DEGRADED
after `aggregate_degrade_after` consecutive all-feeds-down passes (default
2), so a one-off simultaneous blip no longer trips it at all. Per-feed rows,
the GapMonitor, and all dead-man logic are unchanged.
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

# Realistic browser UA. Some wire services 404/403 anything that looks like
# a bot rather than answering honestly with 403 -- this default gets past
# that without needing a config change. Override per-deployment via
# sources.yaml: rss.user_agent, if a specific publisher still objects.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def aggregate_health(feed_ok: dict, fail_streak: int, degrade_after: int,
                     interval: float):
    """Decide the aggregate `ingestion:rss` health from the current per-feed
    state. Pure function (no DB) so it can be unit-tested directly.

    Returns (status, detail) to write, or None to leave the row untouched.
    None is returned only while EVERY feed is down but we are still inside
    the transient-tolerance window (fail_streak < degrade_after): we neither
    falsely report OK nor prematurely flip to red on a one-off blip.

      * at least one feed healthy   -> ("OK", ...)   [clears any old latch]
      * all feeds down, within tol  -> None          [leave prior status]
      * all feeds down >= threshold -> ("DEGRADED", "all feeds failing (xN)")
    """
    total = len(feed_ok)
    if total == 0:
        return None
    down = [n for n, ok in feed_ok.items() if not ok]
    if len(down) == total:
        # Every configured feed is currently failing.
        if fail_streak >= degrade_after:
            return ("DEGRADED", f"all feeds failing (x{fail_streak})")
        return None  # within tolerance — leave the last-written status
    # At least one feed is healthy -> the aggregate source is up.
    if down:
        return ("OK", f"{total - len(down)}/{total} feeds OK")
    return ("OK", f"{total} feeds, every {int(interval)}s")


class RssSource:
    def __init__(self, cfg: dict, monitor: GapMonitor):
        self.tier = int(cfg.get("tier", 3))
        self.interval = float(cfg.get("poll_interval_secs", 60))
        self.feeds = list(cfg.get("feeds", []))
        self.monitor = monitor
        self.user_agent = cfg.get("user_agent") or DEFAULT_USER_AGENT
        self.aggregate_degrade_after = int(cfg.get("aggregate_degrade_after", 2))
        self._cache: dict[str, dict] = {}     # feed name -> {etag, last_modified}
        self._feed_ok: dict[str, bool] = {f["name"]: True for f in self.feeds}
        self._agg_fail_streak = 0

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                     headers={"User-Agent": self.user_agent}) as client:
            await set_health(COMPONENT, "OK", f"{len(self.feeds)} feeds, every {self.interval}s")
            while True:
                for feed in self.feeds:
                    name = feed["name"]
                    try:
                        await self._poll(client, feed)
                        if not self._feed_ok.get(name, True):
                            log.info("feed recovered", extra=kv(feed=name))
                        self._feed_ok[name] = True
                        await set_health(f"{COMPONENT}:{name}", "OK", "polled")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self._feed_ok[name] = False
                        log.error("poll failed", extra=kv(feed=name, error=repr(e)[:200]))
                        await set_health(f"{COMPONENT}:{name}", "DEGRADED", repr(e)[:200])
                    await asyncio.sleep(0.5)
                # Aggregate row (v0.11.11): recomputed every cycle so a
                # recovery clears it, and only DEGRADED after
                # `aggregate_degrade_after` consecutive all-feeds-down passes.
                if self.feeds and not any(self._feed_ok.values()):
                    self._agg_fail_streak += 1
                else:
                    self._agg_fail_streak = 0
                decision = aggregate_health(self._feed_ok, self._agg_fail_streak,
                                            self.aggregate_degrade_after, self.interval)
                if decision is not None:
                    await set_health(COMPONENT, decision[0], decision[1])
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
