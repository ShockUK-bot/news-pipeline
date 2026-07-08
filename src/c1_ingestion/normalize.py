"""C1 normalization: raw source payloads -> validated NewsItem, or a
QuarantineItem with a reason code (v0.4: quarantine, never drop).

One function per source family. Each returns NewsItem on success and raises
NormalizeError(reason_code, detail) on failure; the caller quarantines.
The item is the immutable record of what the feed said — symbols come only
from feed tags, never inference (A1's job, Phase 2).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import ValidationError

from common.clock import parse_ts, utcnow
from common.contracts import NewsItem, content_hash


class NormalizeError(Exception):
    def __init__(self, reason_code: str, detail: str, raw=None, raw_text: str | None = None):
        self.reason_code = reason_code
        self.detail = detail[:500]
        self.raw = raw if isinstance(raw, dict) else None
        self.raw_text = raw_text or (None if isinstance(raw, dict) else repr(raw)[:2000])
        super().__init__(f"{reason_code}: {detail}")


MAX_RAW_BYTES = 512 * 1024  # OVERSIZE guard


def _require(payload: dict, key: str):
    if key not in payload or payload[key] in (None, ""):
        raise NormalizeError("MISSING_REQUIRED_FIELD", f"missing {key}", raw=payload)
    return payload[key]


def _ts_or_quarantine(payload: dict, value, field: str):
    try:
        return parse_ts(value)
    except ValueError as e:
        raise NormalizeError("BAD_TIMESTAMP", f"{field}: {e}", raw=payload)


def _build(payload: dict, **kwargs) -> NewsItem:
    try:
        return NewsItem(**kwargs)
    except ValidationError as e:
        raise NormalizeError("UNKNOWN_SCHEMA", f"contract validation: {e.errors()[:3]}", raw=payload)


# ---------------------------------------------------------------------------
# Alpaca news websocket (v1beta1/news). Message shape:
# {"T":"n","id":40892639,"headline":"...","summary":"...","author":"...",
#  "created_at":"...","updated_at":"...","symbols":["AAPL"],"url":"...",
#  "content":"...","source":"benzinga"}
# ---------------------------------------------------------------------------

def normalize_alpaca(payload: dict, tier: int = 2) -> NewsItem:
    if not isinstance(payload, dict):
        raise NormalizeError("UNPARSEABLE_JSON", "non-object message", raw_text=repr(payload)[:2000])
    if len(str(payload)) > MAX_RAW_BYTES:
        raise NormalizeError("OVERSIZE", f"payload > {MAX_RAW_BYTES}B", raw_text=str(payload)[:2000])

    alpaca_id = _require(payload, "id")
    headline = str(_require(payload, "headline")).strip()
    if not headline:
        raise NormalizeError("MISSING_REQUIRED_FIELD", "empty headline", raw=payload)

    created = _ts_or_quarantine(payload, _require(payload, "created_at"), "created_at")
    summary = (payload.get("summary") or "").strip() or None
    body = (payload.get("content") or "").strip() or None
    symbols = payload.get("symbols") or []
    if not isinstance(symbols, list):
        raise NormalizeError("UNKNOWN_SCHEMA", f"symbols not a list: {type(symbols)}", raw=payload)

    return _build(
        payload,
        item_id=f"alpaca:{alpaca_id}",
        source="alpaca_benzinga",
        source_tier=tier,
        source_url=payload.get("url") or None,
        author=payload.get("author") or None,
        headline=headline,
        summary=summary,
        content_hash=content_hash(headline, summary, body),
        raw=payload,
        symbols=[str(s) for s in symbols],
        channels=[],
        published_ts=created,
        received_ts=utcnow(),
    )


# ---------------------------------------------------------------------------
# SEC EDGAR current-events Atom entries (parsed by feedparser upstream).
# entry: {id, title, link, updated, summary, ...}; title like
# "8-K - ACME CORP (0001234567) (Filer)"
# ---------------------------------------------------------------------------

_EDGAR_TITLE = re.compile(r"^\s*([A-Z0-9/\-]+)\s+-\s+(.*?)\s*(?:\((\d{10})\))?\s*(?:\(.*\))?\s*$")
_ACCESSION = re.compile(r"accession[-_]?number=([\d\-]+)", re.IGNORECASE)


def normalize_edgar(entry: dict, tier: int = 1) -> NewsItem:
    if not isinstance(entry, dict):
        raise NormalizeError("UNPARSEABLE_JSON", "non-object entry", raw_text=repr(entry)[:2000])

    title = str(_require(entry, "title")).strip()
    link = entry.get("link") or ""
    entry_id = str(entry.get("id") or "").strip()

    m = _ACCESSION.search(entry_id) or _ACCESSION.search(link)
    if m:
        item_id = f"edgar:{m.group(1)}"
    elif entry_id:
        item_id = f"edgar:{entry_id[-80:]}"
    else:
        raise NormalizeError("MISSING_REQUIRED_FIELD", "no accession number or entry id", raw=entry)

    updated = _ts_or_quarantine(entry, _require(entry, "updated"), "updated")

    channels = ["filing"]
    form_match = _EDGAR_TITLE.match(title)
    if form_match:
        form = form_match.group(1).upper()
        channels.append(f"form:{form}")
        if form.startswith("8-K"):
            channels.append("8-K")
    # Friday-after-close flag (baseline §4 C1): stamped as a channel so the
    # router/A1 see it without re-deriving.
    et = updated.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    if et.weekday() == 4 and et.hour >= 16:
        channels.append("friday_pm")

    summary = (entry.get("summary") or "").strip() or None

    return _build(
        entry,
        item_id=item_id,
        source="edgar",
        source_tier=tier,
        source_url=link or None,
        headline=title,
        summary=summary,
        content_hash=content_hash(title, summary),
        raw={k: str(v)[:2000] for k, v in entry.items() if isinstance(v, (str, int, float, bool))},
        symbols=[],            # EDGAR entries carry CIK, not ticker; mapping is A1/symbol_map territory
        channels=channels,
        published_ts=updated,
        received_ts=utcnow(),
    )


# ---------------------------------------------------------------------------
# Generic RSS entries (parsed by feedparser upstream)
# ---------------------------------------------------------------------------

def normalize_rss(entry: dict, feed_name: str, tier: int = 3) -> NewsItem:
    if not isinstance(entry, dict):
        raise NormalizeError("UNPARSEABLE_JSON", "non-object entry", raw_text=repr(entry)[:2000])

    title = str(_require(entry, "title")).strip()
    guid = str(entry.get("id") or entry.get("guid") or entry.get("link") or "").strip()
    if not guid:
        raise NormalizeError("MISSING_REQUIRED_FIELD", "no guid/link for stable item_id", raw=entry)

    published = entry.get("published") or entry.get("updated")
    if not published:
        raise NormalizeError("MISSING_REQUIRED_FIELD", "no published/updated timestamp", raw=entry)
    pub_ts = _ts_or_quarantine(entry, published, "published")

    summary = (entry.get("summary") or "").strip() or None
    # stable id: hash the guid so item_id length stays bounded
    import hashlib
    gid = hashlib.sha256(guid.encode()).hexdigest()[:24]

    return _build(
        entry,
        item_id=f"rss:{feed_name}:{gid}",
        source=f"rss:{feed_name}",
        source_tier=tier,
        source_url=entry.get("link") or None,
        author=entry.get("author") or None,
        headline=title,
        summary=summary,
        content_hash=content_hash(title, summary),
        raw={k: str(v)[:2000] for k, v in entry.items() if isinstance(v, (str, int, float, bool))},
        symbols=[],
        channels=[t.get("term", "") for t in entry.get("tags", []) if isinstance(t, dict)][:8],
        published_ts=pub_ts,
        received_ts=utcnow(),
    )
