"""Routing facts — all computed by CODE, never by the model (spec §6).

market_open: pandas-market-calendars NYSE schedule (adopted in Phase 2 by
  decision — holiday-correct from day one). Schedules are cached per day.
position_ids: open positions in journal.positions intersecting the tickers.
  Correct code that returns [] until Phase 4 creates positions.
thesis_matches: ACTIVE theses (Phase 8 store) whose beneficiary tickers
  intersect the signal's tickers — read from the journal.thesis_watchlist
  view. Defensive: any store error degrades to [] so the intraday path
  never depends on the Phase-8 tables.
priority_score: deterministic formula; weights from config/a1.yaml are
  PLACEHOLDERS pending the Phase-4-gating config-values design item.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from common.clock import utcnow
from common.db import get_pool
from common.log import get_logger

log = get_logger("router.facts")

_schedule_cache: dict[str, tuple[datetime, datetime] | None] = {}


def market_open_now(now: datetime | None = None) -> bool:
    """NYSE regular session check, holiday-aware."""
    import pandas_market_calendars as mcal
    now = now or utcnow()
    day_key = now.strftime("%Y-%m-%d")
    if day_key not in _schedule_cache:
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=day_key, end_date=day_key)
        if sched.empty:
            _schedule_cache[day_key] = None          # holiday/weekend
        else:
            _schedule_cache[day_key] = (
                sched.iloc[0]["market_open"].to_pydatetime(),
                sched.iloc[0]["market_close"].to_pydatetime(),
            )
    window = _schedule_cache[day_key]
    if window is None:
        return False
    return window[0] <= now < window[1]


async def open_position_ids(tickers: list[str]) -> list[int]:
    """Open positions intersecting the tickers. Empty until Phase 4."""
    if not tickers:
        return []
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT position_id FROM journal.positions
               WHERE ticker = ANY(%s) AND status = 'OPEN'""",
            (tickers,))
        return [r[0] for r in await cur.fetchall()]


async def thesis_matches(tickers: list[str]) -> list[str]:
    """ACTIVE standing theses (Phase 8 store) matching the tickers, ranked
    by confidence. Journaled in the TRIAGE payload and read by A2's context
    pack and A4. Degrades to [] on any store error — routing rules never
    depend on this fact, so the intraday path is protected."""
    if not tickers:
        return []
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT DISTINCT thesis_id, max(confidence) AS conf
                   FROM journal.thesis_watchlist WHERE ticker = ANY(%s)
                   GROUP BY thesis_id ORDER BY conf DESC""",
                (tickers,))
            return [r[0] for r in await cur.fetchall()]
    except Exception as e:                       # store missing/unmigrated
        log.warning("thesis_matches degraded to []: %s", repr(e)[:150])
        return []


def priority_score(source_tier: int, urgency: str, novelty: float,
                   independent_outlets: int, cfg: dict) -> int:
    tier_w = cfg["tier_weight"].get(source_tier, 0)
    urg_w = cfg["urgency_weight"].get(urgency, 0)
    nov = round(novelty * 4)
    corro = min((max(independent_outlets, 1) - 1) * cfg["corroboration_bonus_per_outlet"],
                cfg["corroboration_bonus_cap"])
    return int(tier_w + urg_w + nov + corro)


@dataclass
class RoutingFacts:
    market_open: bool
    position_ids: list[int] = field(default_factory=list)
    thesis_matches: list[str] = field(default_factory=list)
    priority_score: int = 0

    def payload(self) -> dict:
        return {"market_open": self.market_open, "position_ids": self.position_ids,
                "thesis_matches": self.thesis_matches,
                "priority_score": self.priority_score}


async def compute_facts(tickers: list[str], source_tier: int, urgency: str,
                        novelty: float, independent_outlets: int,
                        router_cfg: dict, now: datetime | None = None) -> RoutingFacts:
    return RoutingFacts(
        market_open=market_open_now(now),
        position_ids=await open_position_ids(tickers),
        thesis_matches=await thesis_matches(tickers),
        priority_score=priority_score(source_tier, urgency, novelty,
                                      independent_outlets, router_cfg),
    )

