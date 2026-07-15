"""C1 storage. The critical invariant: the news_items insert and the
signal.dedup enqueue happen in ONE transaction — a crash between "stored" and
"enqueued" is impossible (rule 19's at-least-once starts here).

Revision logic (v0.4 corrections):
  * new item_id                          -> revision 1
  * existing item_id, same content_hash  -> no-op (feed replay/reconnect echo)
  * existing item_id, changed hash       -> revision N+1, supersedes=N,
                                            is_correction=True
Dedup key for the queue is "{item_id}:{revision}" per spec §3, so each
revision flows through the pipeline exactly once.

Immutable sources (2026-07-14 EDGAR revision-storm fix): for sources whose
identity key already guarantees content identity (EDGAR accession numbers —
a filing never changes; amendments are new accessions), the caller passes
immutable=True and ANY re-seen item_id is a no-op, hash comparison skipped.
Rationale: EDGAR's index lists one filing once per associated entity
(Filer/Subject/Filed-by rows). Per-entity rows alternate content under the
same accession, defeating latest-hash comparison and minting a revision on
every poll cycle (observed: rev 58+ on a single 13G, ~80k items/day).
Hash-based revisioning remains the default for genuinely revisable sources.

enqueue=False stores the item as a record without entering it into the
pipeline (form-whitelist down-routing: Form 4 / 424B2 etc. are kept for the
archive but never cost dedup or triage inference).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from common.contracts import NewsItem, envelope, CONTRACT_DEDUPED
from common.db import get_pool, jb
from common.log import get_logger, kv
from common.queue import enqueue as _enqueue

from .normalize import NormalizeError

log = get_logger("c1.store")

DEDUP_QUEUE = "signal.dedup"


@dataclass
class StoreResult:
    stored: bool                 # False = duplicate echo, nothing written
    revision: int
    is_correction: bool
    enqueued: bool


async def store_item(item: NewsItem, *, immutable: bool = False,
                     enqueue: bool = True) -> StoreResult:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            # Lock the item's revision history to serialize concurrent revisions
            cur = await conn.execute(
                """SELECT revision, content_hash FROM news.news_items
                   WHERE item_id = %s ORDER BY revision DESC LIMIT 1
                   FOR UPDATE""",
                (item.item_id,),
            )
            latest = await cur.fetchone()

            if latest is None:
                revision, supersedes, is_corr = 1, None, item.is_correction
            else:
                prev_rev, prev_hash = latest
                if immutable or prev_hash == item.content_hash:
                    return StoreResult(stored=False, revision=prev_rev,
                                       is_correction=False, enqueued=False)
                revision, supersedes, is_corr = prev_rev + 1, prev_rev, True

            await conn.execute(
                """INSERT INTO news.news_items
                   (item_id, revision, is_correction, supersedes, source, source_tier,
                    source_url, author, headline, summary, content_hash, raw,
                    symbols, channels, lang, published_ts, received_ts)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (item.item_id, revision, is_corr, supersedes, item.source,
                 item.source_tier, item.source_url, item.author, item.headline,
                 item.summary, item.content_hash,
                 jb(item.raw) if item.raw is not None else None,
                 item.symbols, item.channels, item.lang,
                 item.published_ts, item.received_ts),
            )

            enqueued = False
            if enqueue:
                body = item.model_copy(update={
                    "revision": revision, "is_correction": is_corr, "supersedes": supersedes,
                }).payload()
                msg = envelope(CONTRACT_DEDUPED, "C1", item.item_id, item.item_id,
                               revision, body)
                enqueued = await _enqueue(DEDUP_QUEUE, f"{item.item_id}:{revision}",
                                          msg, conn=conn)

    log.info("stored", extra=kv(item_id=item.item_id, revision=revision,
                                correction=is_corr, enqueued=enqueued))
    return StoreResult(stored=True, revision=revision, is_correction=is_corr, enqueued=enqueued)


async def quarantine(err: NormalizeError, source: str) -> None:
    """v0.4: malformed input is kept, never dropped. C7 alerts on rate spikes."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO news.quarantine (source, reason_code, detail, raw, raw_text)
               VALUES (%s,%s,%s,%s,%s)""",
            (source, err.reason_code, err.detail,
             jb(err.raw) if err.raw is not None else None, err.raw_text),
        )
    log.warning("quarantined", extra=kv(source=source, reason=err.reason_code, detail=err.detail))

