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

v0.13.3 — the transport and the trust model. Four changes, all optional-config
with behaviour-preserving defaults:

  1. **Split timeouts, per feed.** The client had a flat 20s covering connect
     and read together, for every feed. A big wire feed at a busy moment
     routinely takes longer than that to send its body, which is the
     `ReadTimeout` that has been logging ERROR against globenewswire. Now:
     connect 10s / read 25s by default (`rss.connect_timeout_secs`,
     `rss.read_timeout_secs`), overridable on any single feed entry.

  2. **Retry inside one poll.** A single timeout used to be a failed poll.
     Now a poll makes `rss.retries` extra attempts (default 1) with a short
     backoff, for transport errors and for the status codes that are usually
     transient or bot-mitigation rather than truth (403/404/408/425/429/5xx).
     404 is in that list deliberately: PR Newswire's 404s in July 2026 turned
     out to be transient (see patch notes v0.11.2), and the wires answer a
     rate-limited client with 404 as often as with 429.

  3. **Per-feed failure tolerance.** The per-feed row used to go DEGRADED and
     log ERROR on the first failed poll. It now needs `rss.feed_degrade_after`
     consecutive failures (default 3) — first failures log WARNING and leave
     the row alone. This is the same shape as v0.11.7 (EDGAR) and v0.11.11
     (the RSS aggregate), applied to the last place that lacked it.

  4. **Content assertions, because status codes lie.** Three failure modes
     found by probing the live feeds on 2026-08-15, none of which a status
     check can see:
       * PR Newswire returns valid RSS with 20 fresh items for a category
         that DOES NOT EXIST — the only tell is an empty channel <title>.
         `expect_title_prefix` asserts the channel title.
       * GlobeNewswire returns valid RSS with ZERO items for a subject code
         that doesn't exist, echoing whatever feedTitle you asked for.
         `require_items` asserts entries > 0.
       * The WSJ markets feed returns perfectly valid RSS whose newest item
         is from January 2025 — structurally healthy, editorially frozen for
         19 months. `stale_after_hours` asserts freshness.
     Title and item assertions are FETCH-EQUIVALENT failures (they mean we
     did not get the feed we asked for). Staleness is deliberately NOT: the
     fetch worked, so `mark_activity()` still fires and the aggregate stays
     green — it only paints that one feed's own row yellow with a `stale:`
     detail. A publisher going quiet must never look like ingestion dying.

  5. **Structured symbols, where the feed publishes them.** `symbol_fields`
     names entry fields to read tickers from, and `adapter` handles feeds
     whose item shape isn't ordinary RSS. Only `nasdaq_halts` exists today.
     This does NOT infer tickers from text — that is still A1's job. It
     reads a dedicated machine-readable field the publisher provides
     (<ndaq:IssueSymbol>), which is a feed tag in the sense of the
     normalize.py doctrine, not inference.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
import httpx

from common.clock import parse_ts, utcnow
from common.log import get_logger, kv
from c1_ingestion.heartbeat import GapMonitor, set_health
from c1_ingestion.normalize import NormalizeError, normalize_rss
from c1_ingestion.store import quarantine, store_item

log = get_logger("c1.rss")

COMPONENT = "ingestion:rss"

_ET = ZoneInfo("America/New_York")

# Realistic browser UA. Some wire services 404/403 anything that looks like
# a bot rather than answering honestly with 403 -- this default gets past
# that without needing a config change. Override per-deployment via
# sources.yaml: rss.user_agent, if a specific publisher still objects.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 25.0
DEFAULT_RETRIES = 1
DEFAULT_RETRY_BASE_SECS = 1.5
DEFAULT_FEED_DEGRADE_AFTER = 3

# Statuses worth one more attempt inside the same poll. 404 is here on
# purpose (see module docstring): the wires use it for bot mitigation.
RETRYABLE_STATUS = frozenset({403, 404, 408, 425, 429, 500, 502, 503, 504})


class FeedContentError(Exception):
    """The response parsed, but it is not the feed we asked for (wrong
    channel title, or empty when this feed is never empty). Treated exactly
    like a fetch failure — see module docstring item 4."""


# ---------------------------------------------------------------------------
# Pure decision helpers. No DB, no network, no clock of their own — all of
# these are unit-tested directly in tests/unit/test_rss_hardening.py.
# ---------------------------------------------------------------------------

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


def feed_health(fails: int, degrade_after: int, last_error: str | None = None,
                stale: str | None = None):
    """Decide ONE feed's `ingestion:rss:<name>` row. Same shape as
    regime_health (v0.11.11) — the last place in C1 that still went red on a
    single miss.

      * fails == 0, fresh          -> ("OK", "polled")
      * fails == 0, stale          -> ("DEGRADED", "stale: ...")   [fetch fine]
      * 0 < fails < degrade_after  -> None      [transient; leave row alone]
      * fails >= degrade_after     -> ("DEGRADED", "<error> (xN)")

    Staleness only applies on a successful poll: a fetch failure is the more
    important thing to say, and an unreachable feed is stale by definition.
    """
    if fails <= 0:
        if stale:
            return ("DEGRADED", f"stale: {stale}"[:200])
        return ("OK", "polled")
    if fails < degrade_after:
        return None
    return ("DEGRADED", f"{last_error or 'poll failed'} (x{fails})"[:200])


def timeout_for(cfg: dict, feed: dict) -> httpx.Timeout:
    """Split connect/read timeout, feed entry overriding the rss block.

    The old flat 20s covered connect AND read together, so a feed that
    connected instantly but took 22s to send a large body raised ReadTimeout
    and logged an ERROR — which is the globenewswire noise this release is
    mostly about."""
    def pick(key: str, default: float) -> float:
        val = feed.get(key, cfg.get(key, default))
        try:
            val = float(val)
        except (TypeError, ValueError):
            return default
        return val if val > 0 else default

    connect = pick("connect_timeout_secs", DEFAULT_CONNECT_TIMEOUT)
    read = pick("read_timeout_secs", DEFAULT_READ_TIMEOUT)
    return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)


def is_retryable(exc: BaseException) -> bool:
    """One more attempt inside this poll, or accept the failure?

    Retryable: any transport-level error (timeout, connect, read, protocol)
    and the status codes in RETRYABLE_STATUS. NOT retryable: content
    assertion failures (a wrong-title feed will be wrong-title again 1.5s
    later) and anything unexpected."""
    if isinstance(exc, FeedContentError):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return isinstance(exc, httpx.TransportError)


def retry_delay(attempt: int, base: float = DEFAULT_RETRY_BASE_SECS) -> float:
    """Backoff before attempt N (1-based). Deterministic — no jitter, because
    a single poller against a handful of feeds has no thundering herd to
    avoid and deterministic timing is easier to reason about in the logs."""
    return round(base * (2 ** max(0, attempt - 1)), 3)


def content_errors(feed: dict, channel: dict, entry_count: int) -> list[str]:
    """Assertions that a 200 OK does not cover. See module docstring item 4.

    Both checks are opt-in per feed, so any feed without the keys behaves
    byte-for-byte as it did before this release."""
    problems: list[str] = []

    prefix = feed.get("expect_title_prefix")
    if prefix:
        title = str((channel or {}).get("title") or "").strip()
        if not title:
            problems.append(
                f"channel title is EMPTY (expected to start with {prefix!r}) — "
                "publisher is serving a fallback feed, not the one requested")
        elif not title.lower().startswith(str(prefix).lower()):
            problems.append(
                f"channel title {title[:60]!r} does not start with {prefix!r}")

    if feed.get("require_items") and entry_count == 0:
        problems.append("feed returned 0 items but require_items is set — "
                        "usually a retired/unknown feed code")
    return problems


def newest_published(entries: list) -> datetime | None:
    """Newest parseable published/updated timestamp across entries, or None.
    Unparseable entries are ignored here — normalize_rss is what quarantines
    those, and a freshness check should not be the thing that fails on one
    bad date."""
    newest: datetime | None = None
    for entry in entries or []:
        raw = (entry.get("published") or entry.get("updated")
               if isinstance(entry, dict) else None)
        if not raw:
            continue
        try:
            ts = parse_ts(raw)
        except ValueError:
            continue
        if newest is None or ts > newest:
            newest = ts
    return newest


def staleness(feed: dict, newest: datetime | None, now: datetime) -> str | None:
    """Human-readable staleness detail, or None if fresh / not configured.

    Opt-in via `stale_after_hours`. A feed that returned no parseable dates
    at all is NOT called stale — that is a different fault and would produce
    a misleading dashboard message."""
    hours = feed.get("stale_after_hours")
    if not hours:
        return None
    try:
        limit = float(hours)
    except (TypeError, ValueError):
        return None
    if newest is None:
        return None
    age = now - newest
    if age <= timedelta(hours=limit):
        return None
    days = age.total_seconds() / 86400.0
    return (f"newest item {days:.1f}d old (limit {limit:g}h) — "
            f"feed answers but is not publishing")


def symbols_from(feed: dict, entry: dict) -> list[str]:
    """Tickers from dedicated machine-readable entry fields named by the
    feed's `symbol_fields`. Never parses free text — inference stays A1's
    job (normalize.py module docstring)."""
    out: list[str] = []
    for field in (feed.get("symbol_fields") or []):
        val = entry.get(str(field))
        if not val:
            continue
        for part in str(val).replace(";", ",").split(","):
            sym = part.strip().upper()
            if sym and sym not in out and len(sym) <= 12:
                out.append(sym)
    return out[:8]


def _halt_published(halt_date: str, now: datetime) -> datetime:
    """Timestamp for a Nasdaq halt item.

    Deliberately NOT built from <ndaq:HaltTime>: that field carries no
    timezone, and probing on 2026-08-15 showed several unrelated symbols
    sharing an identical millisecond value, which is a batch artefact rather
    than a per-symbol halt instant. Guessing a zone risks being 4-5 hours
    wrong in a pipeline where event time drives intraday behaviour.

    Instead: an item first seen on the day it halted is stamped with the
    receive time (accurate to the poll interval, which is 60s), and anything
    from an earlier ET date is stamped at that date's ET midnight so the
    startup backlog can never masquerade as fresh. The raw HaltDate/HaltTime
    are preserved in `raw` for whoever wants to interpret them later."""
    try:
        month, day, year = (int(p) for p in str(halt_date).split("/"))
        halt_day = datetime(year, month, day, tzinfo=_ET)
    except (ValueError, TypeError):
        return now
    if halt_day.date() >= now.astimezone(_ET).date():
        return now
    return halt_day.astimezone(timezone.utc)


def adapt_nasdaq_halt(entry: dict, now: datetime) -> tuple[dict, list[str]]:
    """Rewrite one <item> of nasdaqtrader.com's tradehalts feed into an
    ordinary RSS entry.

    That feed needs an adapter because its items have NO <guid> and NO
    <link> — normalize_rss would quarantine every single one — and its
    <title> is the bare ticker, which is not a headline. Everything the item
    does carry lives in an ndaq: namespace, which feedparser flattens to
    ndaq_issuesymbol, ndaq_haltdate, and so on."""
    sym = str(entry.get("ndaq_issuesymbol") or entry.get("title") or "").strip().upper()
    halt_date = str(entry.get("ndaq_haltdate") or "").strip()
    if not sym or not halt_date:
        raise ValueError("nasdaq halt item missing IssueSymbol/HaltDate")

    halt_time = str(entry.get("ndaq_halttime") or "").strip()
    reason = str(entry.get("ndaq_reasoncode") or "").strip()
    name = str(entry.get("ndaq_issuename") or "").strip()
    market = str(entry.get("ndaq_market") or "").strip()
    resume_trade = str(entry.get("ndaq_resumptiontradetime") or "").strip()

    headline = f"Trading halt: {sym}"
    if name:
        headline += f" — {' '.join(name.split())}"
    if reason:
        headline += f" [{reason}]"
    if resume_trade:
        headline += f" (resumption {resume_trade})"

    summary_bits = [b for b in (
        f"market={market}" if market else "",
        f"halted={halt_date} {halt_time}".strip() if halt_date else "",
        f"reason_code={reason}" if reason else "",
        f"resumption_trade_time={resume_trade}" if resume_trade else "",
    ) if b]

    # Stable across polls, unique per halt event: the same symbol halting
    # twice in a day differs by time and/or reason code.
    guid = f"nasdaq-halt:{sym}:{halt_date}:{halt_time}:{reason}"

    adapted = dict(entry)
    adapted.update({
        "title": headline,
        "id": guid,
        "guid": guid,
        "link": "https://www.nasdaqtrader.com/trader.aspx?id=TradeHalts",
        "summary": "; ".join(summary_bits) or None,
        "published": _halt_published(halt_date, now),
    })
    return adapted, [sym]


def adapt_entry(feed: dict, entry: dict, now: datetime) -> tuple[dict, list[str]]:
    """(entry, symbols) for one raw feedparser entry. Feeds with no `adapter`
    and no `symbol_fields` come back untouched with no symbols — i.e. exactly
    pre-v0.13.3 behaviour."""
    adapter = feed.get("adapter")
    if adapter == "nasdaq_halts":
        return adapt_nasdaq_halt(entry, now)
    if adapter:
        raise ValueError(f"unknown rss adapter {adapter!r}")
    return entry, symbols_from(feed, entry)


def poll_headers(feed: dict, cache: dict) -> dict:
    """Per-request headers for one feed poll. Unit-tested directly.

    v0.12.25: per-feed User-Agent override. The block-level UA is a
    browser impersonation because several PR wires 404/403 honest bots
    (v0.11.1). BLS is the exact opposite: a government server that 403s
    fake browsers and admits only clients identifying themselves WITH
    CONTACT INFO (probe-proven 2026-08-11: plain product string 403,
    repo-URL string 403, email-contact string 200). One global UA cannot
    satisfy both publishers, so a feed may carry its own — request-level
    headers override the client default in httpx.

    Two spellings, because the working string contains an email address
    and this config is in a PUBLIC repo (rule 22 applies to personal data
    as much as to keys):
      user_agent:     literal string in sources.yaml (fine for anything
                      non-personal)
      user_agent_env: NAME of an environment variable holding the string
                      (set in /etc/pipeline/pipeline.env). Takes
                      precedence. Unset/empty env -> fall through to any
                      literal, else no UA header (browser default), which
                      for BLS means a VISIBLE per-feed DEGRADED row - a
                      loud, harmless failure mode.
    Feeds with neither key behave byte-for-byte as before."""
    import os
    headers: dict = {}
    ua = ""
    if feed.get("user_agent_env"):
        ua = os.environ.get(str(feed["user_agent_env"]), "").strip()
    if not ua and feed.get("user_agent"):
        ua = str(feed["user_agent"])
    if ua:
        headers["User-Agent"] = ua
    if cache.get("etag"):
        headers["If-None-Match"] = cache["etag"]
    if cache.get("last_modified"):
        headers["If-Modified-Since"] = cache["last_modified"]
    return headers


class RssSource:
    def __init__(self, cfg: dict, monitor: GapMonitor):
        self.cfg = dict(cfg)
        self.tier = int(cfg.get("tier", 3))
        self.interval = float(cfg.get("poll_interval_secs", 60))
        self.feeds = list(cfg.get("feeds", []))
        self.monitor = monitor
        self.user_agent = cfg.get("user_agent") or DEFAULT_USER_AGENT
        self.aggregate_degrade_after = int(cfg.get("aggregate_degrade_after", 2))
        self.feed_degrade_after = int(cfg.get("feed_degrade_after",
                                              DEFAULT_FEED_DEGRADE_AFTER))
        self.retries = max(0, int(cfg.get("retries", DEFAULT_RETRIES)))
        self.retry_base = float(cfg.get("retry_base_secs", DEFAULT_RETRY_BASE_SECS))
        self._cache: dict[str, dict] = {}     # feed name -> {etag, last_modified}
        self._feed_ok: dict[str, bool] = {f["name"]: True for f in self.feeds}
        self._feed_fails: dict[str, int] = {f["name"]: 0 for f in self.feeds}
        self._agg_fail_streak = 0

    async def run(self) -> None:
        # v0.13.3: the client-level timeout is now only a backstop; every
        # request passes its own split connect/read timeout (timeout_for).
        async with httpx.AsyncClient(timeout=httpx.Timeout(DEFAULT_READ_TIMEOUT),
                                     follow_redirects=True,
                                     headers={"User-Agent": self.user_agent}) as client:
            await set_health(COMPONENT, "OK", f"{len(self.feeds)} feeds, every {self.interval}s")
            while True:
                for feed in self.feeds:
                    name = feed["name"]
                    try:
                        stale = await self._poll_with_retries(client, feed)
                        if not self._feed_ok.get(name, True):
                            log.info("feed recovered",
                                     extra=kv(feed=name, after=self._feed_fails.get(name, 0)))
                        self._feed_ok[name] = True
                        self._feed_fails[name] = 0
                        if stale:
                            log.warning("feed stale", extra=kv(feed=name, detail=stale))
                        decision = feed_health(0, self.feed_degrade_after, stale=stale)
                        if decision is not None:
                            await set_health(f"{COMPONENT}:{name}", decision[0], decision[1])
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self._feed_ok[name] = False
                        fails = self._feed_fails.get(name, 0) + 1
                        self._feed_fails[name] = fails
                        detail = repr(e)[:200]
                        # First misses are noise, not news: WARNING until the
                        # failure is repeated enough to mean something.
                        if fails >= self.feed_degrade_after:
                            log.error("poll failed", extra=kv(feed=name, error=detail, x=fails))
                        else:
                            log.warning("poll failed (transient)",
                                        extra=kv(feed=name, error=detail, x=fails))
                        decision = feed_health(fails, self.feed_degrade_after, last_error=detail)
                        if decision is not None:
                            await set_health(f"{COMPONENT}:{name}", decision[0], decision[1])
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

    async def _poll_with_retries(self, client: httpx.AsyncClient, feed: dict) -> str | None:
        """v0.13.3. Returns a staleness detail string (or None) on success;
        re-raises the last exception once the attempts are exhausted."""
        name = feed["name"]
        attempts = self.retries + 1
        last: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._poll(client, feed)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last = e
                if attempt >= attempts or not is_retryable(e):
                    raise
                delay = retry_delay(attempt, self.retry_base)
                log.info("poll retry", extra=kv(feed=name, attempt=attempt,
                                                delay=delay, error=repr(e)[:160]))
                await asyncio.sleep(delay)
        assert last is not None      # unreachable: the loop either returns or raises
        raise last

    async def _poll(self, client: httpx.AsyncClient, feed: dict) -> str | None:
        name, url = feed["name"], feed["url"]
        headers = poll_headers(feed, self._cache.get(name, {}))
        resp = await client.get(url, headers=headers, timeout=timeout_for(self.cfg, feed))
        if resp.status_code == 304:
            self.monitor.mark_activity()
            return None
        resp.raise_for_status()

        parsed = feedparser.parse(resp.text)
        if parsed.bozo and not parsed.entries:
            # Cache is NOT updated here: a malformed body must not become the
            # baseline for the next conditional GET.
            await quarantine(NormalizeError("UNPARSEABLE_JSON",
                                            f"rss parse: {parsed.bozo_exception!r}",
                                            raw_text=resp.text[:2000]), f"rss:{name}")
            return None

        # v0.13.3: assert we got the feed we asked for BEFORE storing anything
        # or advancing the conditional-GET cache. See module docstring item 4.
        problems = content_errors(feed, dict(parsed.feed or {}), len(parsed.entries))
        if problems:
            raise FeedContentError("; ".join(problems))

        self._cache[name] = {"etag": resp.headers.get("ETag"),
                             "last_modified": resp.headers.get("Last-Modified")}

        # v0.12.24: per-feed tier override + static channel tags. The block
        # tier (3) stays the default; official primary sources (Fed, BLS)
        # declare tier 1 on their own feed entry in sources.yaml.
        feed_tier = int(feed.get("tier", self.tier))
        feed_tags = [str(t) for t in (feed.get("tags") or [])]
        now = utcnow()
        stored = 0
        for entry in parsed.entries:
            try:
                adapted, symbols = adapt_entry(feed, dict(entry), now)
            except ValueError as e:
                await quarantine(NormalizeError("UNKNOWN_SCHEMA", f"adapter: {e}",
                                                raw=dict(entry)), f"rss:{name}")
                continue
            try:
                item = normalize_rss(adapted, feed_name=name,
                                     tier=feed_tier,
                                     extra_channels=feed_tags,
                                     symbols=symbols)
                result = await store_item(item)
                if result.stored:
                    stored += 1
            except NormalizeError as e:
                await quarantine(e, f"rss:{name}")
        if stored:
            log.info("poll stored", extra=kv(feed=name, new=stored))
        self.monitor.mark_activity()
        return staleness(feed, newest_published(parsed.entries), now)
