"""C1 heartbeats and ingestion-gap tracking.

Two outputs:
  journal.health   — component status the dashboard and dead-man logic read
  news.ingestion_gaps — explicit "no data 2:14–5:30" rows surfaced to A4/A8

Gap semantics: per source, a Monitor tracks last_item_ts. When silence exceeds
the market-hours-aware threshold, one gap row opens (gap_end NULL while
ongoing); on the next item it closes. Threshold selection uses coarse RTH from
common.clock — a false-positive gap on a holiday is tolerable in Phase 1.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from common.clock import is_market_hours, utcnow
from common.db import get_pool
from common.log import get_logger, kv

log = get_logger("c1.heartbeat")


async def set_health(component: str, status: str, detail: str = "") -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO journal.health (component, status, detail, updated_ts)
               VALUES (%s,%s,%s, now())
               ON CONFLICT (component) DO UPDATE
               SET status = EXCLUDED.status, detail = EXCLUDED.detail,
                   updated_ts = EXCLUDED.updated_ts""",
            (component, status, detail[:500]),
        )


class GapMonitor:
    def __init__(self, source: str, market_threshold_secs: int, offhours_threshold_secs: int):
        self.source = source
        self.market_threshold = market_threshold_secs
        self.offhours_threshold = offhours_threshold_secs
        self.last_item_ts: datetime = utcnow()   # start of monitoring counts as activity
        self.open_gap_id: Optional[int] = None

    def _threshold(self) -> int:
        return self.market_threshold if is_market_hours() else self.offhours_threshold

    def mark_activity(self) -> None:
        self.last_item_ts = utcnow()

    async def check(self) -> None:
        """Called periodically by the watchdog. Opens/closes gap rows."""
        now = utcnow()
        silent = (now - self.last_item_ts).total_seconds()
        pool = await get_pool()

        if self.open_gap_id is None and silent > self._threshold():
            async with pool.connection() as conn:
                cur = await conn.execute(
                    """INSERT INTO news.ingestion_gaps (source, gap_start, detail)
                       VALUES (%s,%s,%s) RETURNING gap_id""",
                    (self.source, self.last_item_ts,
                     f"silent {int(silent)}s (threshold {self._threshold()}s)"),
                )
                self.open_gap_id = (await cur.fetchone())[0]
            log.warning("gap opened", extra=kv(source=self.source, silent_secs=int(silent)))
            await set_health(f"ingestion:{self.source}", "DEGRADED",
                             f"no items for {int(silent)}s")
        elif self.open_gap_id is not None and silent <= self._threshold():
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE news.ingestion_gaps SET gap_end = %s WHERE gap_id = %s",
                    (self.last_item_ts, self.open_gap_id),
                )
            log.info("gap closed", extra=kv(source=self.source, gap_id=self.open_gap_id))
            self.open_gap_id = None
            await set_health(f"ingestion:{self.source}", "OK", "recovered")
