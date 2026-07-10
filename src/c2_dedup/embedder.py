"""Pluggable embedder. EMBEDDER=bge -> sentence-transformers
BAAI/bge-small-en-v1.5 (384-dim; install the [embed] extra and let it download
the model — one-time, ~130MB). EMBEDDER=hash -> deterministic 384-dim bag-of-
token-hashes embedder: no downloads, stable across runs, near-duplicate texts
map to near-identical vectors. Dev/test only — its notion of similarity is
lexical, not semantic, so it under-clusters paraphrases. Production uses bge.

Both are L2-normalized so Qdrant cosine similarity is a dot product and the
0.9 dedup threshold means the same thing in either mode.
"""
from __future__ import annotations

import hashlib
import math
import os
import re

DIM = 384

_TOKEN = re.compile(r"[a-z0-9]{2,}")


class HashEmbedder:
    """Deterministic: sha1(token) -> bucket + sign, L2-normalized."""
    name = "hash-384"

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * DIM
        for tok in _TOKEN.findall(text.lower()):
            h = hashlib.sha1(tok.encode()).digest()
            bucket = int.from_bytes(h[:4], "big") % DIM
            sign = 1.0 if h[4] % 2 == 0 else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class BgeEmbedder:
    name = "bge-small-en-v1.5"

    def __init__(self):
        from sentence_transformers import SentenceTransformer  # [embed] extra
        self._model = SentenceTransformer("BAAI/bge-small-en-v1.5")

    def embed(self, text: str) -> list[float]:
        return self._model.encode(text, normalize_embeddings=True).tolist()


def get_embedder():
    kind = os.environ.get("EMBEDDER", "hash").lower()
    if kind == "bge":
        return BgeEmbedder()
    if kind == "hash":
        return HashEmbedder()
    raise RuntimeError(f"unknown EMBEDDER={kind!r} (expected 'bge' or 'hash')")


def embed_text_for(headline: str, summary: str | None) -> str:
    """What we embed: headline + summary. Body is noisy and slow; the
    dedup/cluster decision is a story-identity question, which the headline
    and lede carry."""
    return headline if not summary else f"{headline}\n{summary}"

