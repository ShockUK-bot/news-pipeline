"""Pipeline contracts as Pydantic models, mirroring queue-contracts-spec.md v1.0.

These are the *code-side* JSON Schema validation the spec requires (§13):
grammar-constrained decoding enforces contracts on the model side (Phase 2+);
this module enforces them on every code hop. A NewsItem that fails validation
never reaches news.news_items — it goes to quarantine with a reason code.
"""
from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .clock import iso_utc

CONTRACT_NEWS_ITEM = "news_item/1"
CONTRACT_DEDUPED = "signal.dedup/1"
CONTRACT_TRIAGE = "signal.triage/1"


# ---------------------------------------------------------------------------
# §4 NewsItem — mirrors news.news_items one-to-one
# ---------------------------------------------------------------------------

class NewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)          # source-scoped: "alpaca:40892639"
    revision: int = Field(default=1, ge=1)
    is_correction: bool = False
    supersedes: Optional[int] = None

    source: str = Field(min_length=1)           # "alpaca_benzinga"|"edgar"|"rss:<feed>"
    source_tier: Literal[1, 2, 3]
    source_url: Optional[str] = None
    author: Optional[str] = None

    headline: str = Field(min_length=1)
    summary: Optional[str] = None
    content_hash: str = Field(min_length=1)
    raw: Optional[dict] = None

    symbols: list[str] = Field(default_factory=list)   # MAY BE EMPTY (v0.2)
    channels: list[str] = Field(default_factory=list)
    lang: str = "en"

    published_ts: datetime                       # the source's claim
    received_ts: datetime                        # our wall clock

    @field_validator("published_ts", "received_ts")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("naive timestamp")
        return v

    @field_validator("symbols")
    @classmethod
    def _upper_symbols(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if s and s.strip()]

    def payload(self) -> dict:
        """JSON-safe dict for queue payloads (ISO-8601 UTC ms timestamps, spec §3)."""
        d = self.model_dump(exclude={"raw"})
        d["published_ts"] = iso_utc(self.published_ts)
        d["received_ts"] = iso_utc(self.received_ts)
        return d


def content_hash(headline: str, summary: str | None = None, body: str | None = None) -> str:
    """sha256 of normalized headline+summary+body (spec §4).

    Normalization: NFKC, casefold, whitespace collapsed — so trivial
    reformatting by a feed doesn't masquerade as a revision.
    """
    parts = []
    for part in (headline, summary, body):
        if part:
            norm = unicodedata.normalize("NFKC", part).casefold()
            parts.append(" ".join(norm.split()))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# §5 DedupedSignal (C2 -> A1)
# ---------------------------------------------------------------------------

class ClusterInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster_id: int
    is_new_story: bool
    independent_outlets: int = Field(ge=1)
    total_items: int = Field(ge=1)
    similarity_to_canonical: float = Field(ge=0.0, le=1.0)


class DedupedSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: dict                    # NewsItem.payload(), latest revision
    cluster: ClusterInfo


# ---------------------------------------------------------------------------
# §3 Common envelope
# ---------------------------------------------------------------------------

def envelope(msg_schema: str, producer: str, signal_id: str, item_id: str,
             revision: int, body: dict) -> dict:
    return {
        "envelope": {
            "msg_schema": msg_schema,
            "produced_ts": iso_utc(),
            "producer": producer,
            "trace": {"signal_id": signal_id, "item_id": item_id, "revision": revision},
        },
        "body": body,
    }

