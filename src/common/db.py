"""psycopg3 async pool + small helpers. One pool per process."""
from __future__ import annotations

import json
import os

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


def dsn() -> str:
    v = os.environ.get("PIPELINE_DSN")
    if not v:
        raise RuntimeError("PIPELINE_DSN is not set (see .env.example)")
    return v


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        # queue.claim_next/ack/fail are plpgsql with unqualified table names —
        # they resolve via the session search_path (the validated lifecycle
        # test sets it the same way). journal/news/queue all on the path.
        _pool = AsyncConnectionPool(
            dsn(), min_size=1, max_size=8, open=False,
            kwargs={"options": "-c search_path=public,journal,news,queue"},
        )
        await _pool.open()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def jb(obj) -> Jsonb:
    """JSONB adapter shorthand."""
    return Jsonb(obj)


def as_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

