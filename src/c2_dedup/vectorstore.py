"""Qdrant wrapper implementing the v0.4 two-collection rule:

  dedup_48h  — every item's vector, pruned to a trailing 48h window (hourly)
  retrieval  — material items only, long retention; admission is A1's material
               flag, so Phase 1 only *implements* promote_to_retrieval() —
               A1's wrapper calls it in Phase 2.

QDRANT_URL set   -> server mode (the Spark's docker container)
QDRANT_URL empty -> qdrant-client local mode persisted at QDRANT_PATH
Identical API either way — the mode is a deployment detail.

Point IDs: uuid5 of "{item_id}:{revision}" (Qdrant requires uuid/int IDs);
the original ids ride in the payload.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (Distance, FieldCondition, Filter, PointStruct,
                                  Range, VectorParams)

from common.clock import utcnow
from common.log import get_logger, kv

log = get_logger("c2.vectorstore")

_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def _pid(item_id: str, revision: int) -> str:
    return str(uuid.uuid5(_NS, f"{item_id}:{revision}"))


@dataclass
class Neighbor:
    item_id: str
    revision: int
    cluster_id: Optional[int]
    score: float


class VectorStore:
    def __init__(self, dedup_collection: str = "dedup_48h",
                 retrieval_collection: str = "retrieval", dim: int = 384):
        url = os.environ.get("QDRANT_URL") or None
        if url:
            self.client = QdrantClient(url=url)
            mode = url
        else:
            path = os.environ.get("QDRANT_PATH", "./qdrant-local")
            self.client = QdrantClient(path=path)
            mode = f"local:{path}"
        self.dedup = dedup_collection
        self.retrieval = retrieval_collection
        self.dim = dim
        for coll in (self.dedup, self.retrieval):
            if not self.client.collection_exists(coll):
                self.client.create_collection(
                    coll, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
        log.info("vector store ready", extra=kv(mode=mode, dim=dim))

    # -- dedup collection ----------------------------------------------------

    def nearest(self, vector: list[float], limit: int = 5) -> list[Neighbor]:
        hits = self.client.query_points(self.dedup, query=vector, limit=limit).points
        return [Neighbor(item_id=h.payload["item_id"], revision=h.payload["revision"],
                         cluster_id=h.payload.get("cluster_id"), score=h.score)
                for h in hits]

    def upsert_dedup(self, item_id: str, revision: int, vector: list[float],
                     cluster_id: int, source: str) -> None:
        self.client.upsert(self.dedup, points=[PointStruct(
            id=_pid(item_id, revision), vector=vector,
            payload={"item_id": item_id, "revision": revision,
                     "cluster_id": cluster_id, "source": source,
                     "ts": utcnow().timestamp()},
        )])

    def prune_dedup(self, window_hours: int = 48) -> int:
        """Trailing-window prune — keeps the collection small and fast forever."""
        cutoff = (utcnow() - timedelta(hours=window_hours)).timestamp()
        flt = Filter(must=[FieldCondition(key="ts", range=Range(lt=cutoff))])
        before = self.client.count(self.dedup, count_filter=flt).count
        if before:
            self.client.delete(self.dedup, points_selector=flt)
            log.info("pruned dedup collection", extra=kv(removed=before))
        return before

    # -- retrieval collection (admission = A1 material flag; Phase 2 caller) --

    def promote_to_retrieval(self, item_id: str, revision: int, vector: list[float],
                             payload: dict) -> None:
        """Copy a material item into the long-retention retrieval collection.
        Called by A1's wrapper when it sets material=true (Phase 2)."""
        self.client.upsert(self.retrieval, points=[PointStruct(
            id=_pid(item_id, revision), vector=vector,
            payload={"item_id": item_id, "revision": revision, **payload},
        )])

    def related(self, vector: list[float], limit: int = 8) -> list[dict]:
        """Related-headline context for A2 (Phase 3 caller) — retrieval only."""
        hits = self.client.query_points(self.retrieval, query=vector, limit=limit).points
        return [{"score": h.score, **h.payload} for h in hits]
