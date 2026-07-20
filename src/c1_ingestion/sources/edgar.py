"""SEC EDGAR current-events poller.

Fair-access compliance:
  * mandatory User-Agent "<EDGAR_APP_NAME> <EDGAR_CONTACT>" — fails fast at
    startup if EDGAR_CONTACT is unset (better than silent 403s at 2 AM)
  * poll_interval_secs from sources.yaml (default 15s, far under the
    10 req/s cap), one feed request per interval per feed
  * honors 429/503 with a longer sleep

Dedup across polls (fixed 2026-07-14 — the revision-storm incident):
  * EDGAR's index lists one filing once PER ASSOCIATED ENTITY (Filer /
    Subject / Filed-by rows share an accession). Entries are grouped by
    accession within each poll and merged into ONE item; all entities land
    in raw["entities"], the canonical headline prefers the Filer/Issuer row.
  * A filing is immutable by definition — store_item(immutable=True) makes
    any re-seen accession an unconditional no-op. No hash comparison, no
    revisions from this path, ever. Amended filings (8-K/A etc.) have new
    accession numbers -> new items, which is correct: an amendment is a new
    filing event, not a revision of the old text.
  * Form whitelist (config: edgar.triage_forms): only event-class filings
    enter the pipeline. Everything else is stored as a record with
    enqueue=False — kept for the archive, never costs dedup or triage
    inference. Matching is by form prefix so "8-K" admits "8-K/A".
  * CIK->ticker symbols (v0.4.6): every item's entity CIKs are mapped
    through SEC's company_tickers.json (cik_map.py) and stamped into
    symbols deterministically — code disposes; A1 no longer infers tickers
    for EDGAR items. With edgar.skip_unmapped true (default), whitelisted
    filings whose entities map to NO listed ticker (trusts, funds, private
    filers) are archived without triage — they cannot produce a long-only
    US equity trade. If the map is empty/unfetchable, skip_unmapped is
    ignored (fail-safe: items flow to A1, whose inference is the fallback).
"""
from __future__ import annotations

import asyncio
import os

import feedparser
import httpx

from common.log import get_logger, kv
from c1_ingestion.heartbeat import GapMonitor, set_health
from c1_ingestion.cik_map import CikMap
from c1_ingestion.normalize import (NormalizeError, edgar_accession,
                                    edgar_title_parts, normalize_edgar)
from c1_ingestion.store import quarantine, store_item

log = get_logger("c1.edgar")

COMPONENT = "ingestion:edgar"

# Event-class filings worth triage inference; prefix match so "8-K" admits
# "8-K/A". Overridable via edgar.triage_forms in sources.yaml. 10-K/10-Q
# included for the long-horizon lane per baseline A5/A6.
DEFAULT_TRIAGE_FORMS = [
    "8-K", "6-K", "S-1", "425",
    "SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G",
    "10-K", "10-Q",
]

# Canonical-headline preference when merging multi-entity rows: the row
# naming the company (Filer/Issuer/Subject) over the person filing about it.
_ROLE_RANK = {"filer": 0, "issuer": 1, "subject": 2, "filed by": 3}


def _role_rank(role: str | None) -> int:
    return _ROLE_RANK.get((role or "").strip().lower(), 9)


def form_whitelisted(form: str | None, whitelist: list[str]) -> bool:
    if not form:
        return False
    f = form.upper().strip()
    return any(f == w or f.startswith(w + "/") or f.startswith(w + " ")
               for w in (w.upper().strip() for w in whitelist))


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
        self.triage_forms = list(cfg.get("triage_forms", DEFAULT_TRIAGE_FORMS))
        self.skip_unmapped = bool(cfg.get("skip_unmapped", True))
        self.cik_map = CikMap(
            cfg.get("cik_map_path", "/opt/pipeline/var/cik_map.json"),
            refresh_hours=float(cfg.get("cik_map_refresh_hours", 24)))
        self.monitor = monitor
        self.ua = user_agent()
        # v0.11.7: tolerate transient SEC slowness. Larger HTTP timeout for the
        # big index feeds, and DEGRADE health only after N consecutive failures
        # of the same feed — a single ReadTimeout no longer paints the dashboard
        # yellow. Both tunable via config.
        self.http_timeout = float(cfg.get("http_timeout_secs", 45.0))
        self.degrade_after = int(cfg.get("degrade_after_failures", 3))
        self._fail_streak: dict[str, int] = {}
        self._last_error: dict[str, str] = {}

    async def run(self) -> None:
        async with httpx.AsyncClient(
            headers={"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"},
            timeout=self.http_timeout, follow_redirects=True,
        ) as client:
            await self.cik_map.ensure_fresh(client)
            await set_health(COMPONENT, "OK",
                             f"polling every {self.interval}s; "
                             f"cik map: {self.cik_map.known()} entities")
            while True:
                await self.cik_map.ensure_fresh(client)   # no-op unless stale
                for feed in self.feeds:
                    name = feed["name"]
                    try:
                        await self._poll(client, feed)
                        self._fail_streak[name] = 0        # recovered
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self._fail_streak[name] = self._fail_streak.get(name, 0) + 1
                        self._last_error[name] = f"{name}: {e!r} (x{self._fail_streak[name]})"
                        log.warning("poll failed", extra=kv(
                            feed=name, streak=self._fail_streak[name], error=repr(e)[:200]))
                    await asyncio.sleep(1.0)      # spacing between feeds within a cycle
                # Aggregate health once per cycle: DEGRADED only for feeds that
                # have failed >= degrade_after consecutive cycles (a transient
                # SEC ReadTimeout self-heals before then).
                degraded = [n for n, s in self._fail_streak.items()
                            if s >= self.degrade_after]
                if degraded:
                    await set_health(COMPONENT, "DEGRADED",
                                     "; ".join(self._last_error[n] for n in degraded)[:200])
                else:
                    await set_health(COMPONENT, "OK",
                                     f"polling every {self.interval}s; "
                                     f"cik map: {self.cik_map.known()} entities")
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

        # Group index rows by accession: one filing = one item, however many
        # associated-entity rows the index shows for it.
        groups: dict[str, list[dict]] = {}
        ungrouped: list[dict] = []
        for entry in parsed.entries:
            e = dict(entry)
            acc = edgar_accession(e)
            if acc:
                groups.setdefault(acc, []).append(e)
            else:
                ungrouped.append(e)          # normalize_edgar falls back to entry id

        stored = skipped_form = 0
        for acc, entries in groups.items():
            try:
                item = self._merge_group(entries)
                allow = self._admit(item)
                result = await store_item(item, immutable=True, enqueue=allow)
                if result.stored:
                    stored += 1
                    if not allow:
                        skipped_form += 1
                    self.monitor.mark_activity()
            except NormalizeError as e:
                await quarantine(e, "edgar")
        for e in ungrouped:
            try:
                item = normalize_edgar(e, tier=self.tier)
                self._stamp_symbols(item)
                allow = self._admit(item)
                result = await store_item(item, immutable=True, enqueue=allow)
                if result.stored:
                    stored += 1
                    self.monitor.mark_activity()
            except NormalizeError as e2:
                await quarantine(e2, "edgar")

        if stored:
            log.info("poll stored", extra=kv(feed=feed["name"], new=stored,
                                             archived_only=skipped_form))
        # a successful poll is liveness even with zero new filings
        self.monitor.mark_activity()

    def _merge_group(self, entries: list[dict]):
        """One NewsItem per accession. Canonical row = best role rank (the
        company over the person); all entity rows preserved in raw."""
        ranked = sorted(entries, key=lambda e: _role_rank(
            edgar_title_parts(str(e.get("title") or ""))[3]))
        item = normalize_edgar(ranked[0], tier=self.tier)
        entities = []
        for e in ranked:
            _, name, cik, role = edgar_title_parts(str(e.get("title") or ""))
            ent = {"name": name or "", "cik": cik or "", "role": role or ""}
            if ent not in entities:
                entities.append(ent)
        if item.raw is not None:
            item.raw["entities"] = entities
        self._stamp_symbols(item)
        return item

    def _stamp_symbols(self, item) -> None:
        """Deterministic ticker stamping from entity CIKs (v0.4.6)."""
        ents = (item.raw or {}).get("entities") or []
        tickers = self.cik_map.tickers_for([e.get("cik") for e in ents if e.get("cik")])
        if tickers:
            item.symbols = tickers

    def _admit(self, item) -> bool:
        """Should this filing enter the pipeline (enqueue to dedup)?
        Form whitelist first; then, if skip_unmapped and the map is usable,
        require at least one mapped ticker — an entity with no listed
        security cannot produce a long-only US equity trade."""
        if not form_whitelisted((item.raw or {}).get("form"), self.triage_forms):
            return False
        if self.skip_unmapped and self.cik_map.known() > 0 and not item.symbols:
            return False
        return True

