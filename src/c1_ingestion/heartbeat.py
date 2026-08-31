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

import time
from datetime import datetime
from typing import Optional

from common.clock import is_market_hours, utcnow
from common.db import get_pool
from common.log import get_logger, kv

log = get_logger("c1.heartbeat")


async def set_health(component: str, status: str, detail: str = "") -> None:
    """Upsert a component's health row. NEVER raises (v0.14.4).

    Before this, a transient database error thrown from a periodic heartbeat
    propagated out of the caller's consume loop and out of main(), killing
    the service outright — the mirror image of the 2026-08-27 hang and just
    as silent. A failed write is now logged and the row simply goes stale,
    which C7 reports as HEARTBEAT_STALE within five minutes. Staleness is a
    signal we can see; a dead process is not.
    """
    try:
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
    except Exception as e:                                    # noqa: BLE001
        log.error("set_health failed — row will go stale",
                  extra=kv(component=component, status=status,
                           error=repr(e)[:200]))


class Heartbeat:
    """Periodic journal.health writer for a long-running consume loop.

    A service that writes its health row only at startup is indistinguishable
    from one that hung days ago. That is precisely how a1-triage stayed
    invisible from 2026-08-27 to 2026-08-31: systemd said active, the health
    row said OK, and both had been true once.

    Call start() before the loop and tick() on every iteration. tick() writes
    at most once per interval and costs one clock read otherwise, so it is
    safe in the hot path.
    """

    def __init__(self, component: str, detail: str = "",
                 interval_secs: float = 60.0) -> None:
        self.component = component
        self.detail = detail
        self.interval = interval_secs
        self._last = 0.0

    async def start(self) -> None:
        await set_health(self.component, "OK", self.detail)
        self._last = time.monotonic()

    async def tick(self, detail: str | None = None) -> None:
        if time.monotonic() - self._last < self.interval:
            return
        await set_health(self.component, "OK", detail or self.detail)
        self._last = time.monotonic()


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

