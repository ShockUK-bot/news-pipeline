"""C2 dedup + cluster decision logic.

For each incoming item revision:
  1. embed headline+summary
  2. nearest neighbor in dedup_48h
  3.   sim >= similarity_threshold (0.90) AND same story already seen
         -> DUPLICATE: join the neighbor's cluster, refresh corroboration,
            forward to A1 only as is_new_story=false (spec §5: A1 invoked only
            if corroboration crossing thresholds matters — that's the router's
            call in Phase 2; C2 always forwards the signal with the flag)
  4.   cluster_threshold (0.80) <= sim < similarity_threshold
         -> RELATED: same story, distinct enough wording to be corroboration
            from another outlet; join cluster, is_new_story=false
  5.   sim < cluster_threshold -> NEW STORY: create cluster, is_new_story=true

A *revision* of an item already in a cluster stays in its cluster (the story
identity didn't change; the text did) — membership rows are per revision.

Corroboration counting (independent outlets) is the cluster_corroboration
view in Postgres; we read it back after every membership write so the
DedupedSignal always carries current numbers (C3's credibility input, v0.2).
"""
from __future__ import annotations

from dataclasses import dataclass

from common.db import get_pool
from common.log import get_logger, kv

from .embedder import embed_text_for
from .vectorstore import VectorStore

log = get_logger("c2.cluster")


@dataclass
class ClusterDecision:
    cluster_id: int
    is_new_story: bool
    independent_outlets: int
    total_items: int
    similarity_to_canonical: float
    # >= similarity_threshold to an existing DIFFERENT item: baseline §4 C2
    # says drop. Membership + corroboration are still recorded (C3 reads the
    # view); the signal just doesn't re-enter the pipeline. Fixed 2026-07-14 —
    # C2 previously computed this verdict, logged it, and forwarded anyway.
    is_duplicate: bool = False


async def _existing_cluster_of(item_id: str) -> int | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT cluster_id FROM news.cluster_members WHERE item_id = %s LIMIT 1",
            (item_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def _create_cluster(canonical_item: str) -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO news.clusters (canonical_item) VALUES (%s) RETURNING cluster_id",
            (canonical_item,))
        return (await cur.fetchone())[0]


async def _add_member(cluster_id: int, item_id: str, revision: int,
                      source: str, similarity: float) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO news.cluster_members
               (cluster_id, item_id, revision, source, similarity)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (cluster_id, item_id, revision) DO NOTHING""",
            (cluster_id, item_id, revision, source, similarity))


async def _corroboration(cluster_id: int) -> tuple[int, int]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT independent_outlets, total_items
               FROM news.cluster_corroboration WHERE cluster_id = %s""",
            (cluster_id,))
        row = await cur.fetchone()
        return (row[0], row[1]) if row else (1, 1)


class Deduper:
    def __init__(self, store: VectorStore, embedder, similarity_threshold: float = 0.90,
                 cluster_threshold: float = 0.80):
        self.store = store
        self.embedder = embedder
        self.sim_threshold = similarity_threshold
        self.cluster_threshold = cluster_threshold

    async def process(self, item: dict) -> ClusterDecision:
        """item: the NewsItem payload dict from the signal.dedup message body."""
        item_id, revision = item["item_id"], item["revision"]
        source = item["source"]
        vector = self.embedder.embed(embed_text_for(item["headline"], item.get("summary")))

        # A revision of a clustered item stays in its cluster.
        existing = await _existing_cluster_of(item_id)
        if existing is not None:
            await _add_member(existing, item_id, revision, source, 1.0)
            self.store.upsert_dedup(item_id, revision, vector, existing, source)
            outlets, total = await _corroboration(existing)
            log.info("revision joined own cluster",
                     extra=kv(item_id=item_id, rev=revision, cluster=existing))
            return ClusterDecision(existing, False, outlets, total, 1.0,
                                   is_duplicate=False)

        neighbors = [n for n in self.store.nearest(vector, limit=3)
                     if not (n.item_id == item_id)]
        best = neighbors[0] if neighbors else None

        is_dup = False
        if best is not None and best.score >= self.cluster_threshold and best.cluster_id:
            cluster_id, is_new = best.cluster_id, False
            sim = float(best.score)
            is_dup = best.score >= self.sim_threshold
            kind = "duplicate" if is_dup else "corroboration"
            log.info(f"joined cluster ({kind})",
                     extra=kv(item_id=item_id, cluster=cluster_id, sim=round(sim, 3)))
        else:
            cluster_id, is_new, sim = await _create_cluster(item_id), True, 1.0
            log.info("new story cluster", extra=kv(item_id=item_id, cluster=cluster_id))

        await _add_member(cluster_id, item_id, revision, source, sim)
        self.store.upsert_dedup(item_id, revision, vector, cluster_id, source)
        outlets, total = await _corroboration(cluster_id)
        return ClusterDecision(cluster_id, is_new, outlets, total, sim,
                               is_duplicate=is_dup)

