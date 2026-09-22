"""Drawdown breaker (phase4-design-v1_0 D7-confirmed: -2% daily).

Day PnL = realized (exits journaled today, UTC session date) + unrealized
(open positions marked at last_price). Trip when day PnL <= -2% of effective
capital: set drawdown_breaker=1 (audited, actor C4). ONE-WAY — code never
resets it; the operator does, from the dashboard, deliberately (runbook §5).
A3 vetoes and C4 pre-flight both already honor the flag; exits continue.
"""
from __future__ import annotations

from common.db import get_pool
from common.log import get_logger, kv

from .flags import breaker_on, get_flag, set_flag

log = get_logger("c4.breaker")


async def day_pnl() -> dict:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT COALESCE(sum(realized_pnl),0) FROM journal.exits
               WHERE ts::date = (now() AT TIME ZONE 'UTC')::date""")
        realized = float((await cur.fetchone())[0])
        # v0.25.1: side-aware. A SHORT in profit used to count as a LOSS here
        # (price minus entry with no sign), so a winning 29k short 3.5% in
        # our favour read as a 1,000 dollar drawdown: a false trip that only
        # the operator could clear.
        cur = await conn.execute(
            """SELECT COALESCE(sum((last_price - avg_entry) * qty_open
                                   * CASE WHEN side='SHORT' THEN -1 ELSE 1 END), 0)
               FROM journal.positions
               WHERE status='OPEN' AND last_price IS NOT NULL""")
        unrealized = float((await cur.fetchone())[0])
    return {"realized": realized, "unrealized": unrealized,
            "total": realized + unrealized}


async def check_breaker(drawdown_pct: float) -> bool:
    """Returns True if the breaker is (now) tripped."""
    if await breaker_on():
        return True
    equity = float(await get_flag("broker_equity", "0") or 0)
    capital = float(await get_flag("trading_capital", "0") or 0)
    effective = min(equity, capital)
    if effective <= 0:
        return False
    pnl = await day_pnl()
    threshold = -drawdown_pct * effective
    if pnl["total"] <= threshold:
        detail = (f"BREAKER_TRIP day_pnl={pnl['total']:.2f} "
                  f"(realized={pnl['realized']:.2f} "
                  f"unrealized={pnl['unrealized']:.2f}) "
                  f"threshold={threshold:.2f}")
        await set_flag("drawdown_breaker", "1", "C4", detail)
        log.warning("drawdown breaker TRIPPED",
                    extra=kv(day_pnl=round(pnl["total"], 2),
                             threshold=round(threshold, 2)))
        await _alert("[breaker] TRIPPED: no new entries until reset",
                     [f"Day P&L {pnl['total']:+.2f} (realized {pnl['realized']:+.2f}, "
                      f"unrealized {pnl['unrealized']:+.2f}) reached the {threshold:+.2f} threshold "
                      f"({drawdown_pct:.1%} of {effective:,.0f}).",
                      "Exits continue; entries are blocked. With breaker_auto_reset enabled the "
                      "flag clears before the next session unless the 30 day trip limit is reached."])
        return True
    return False


# ---------------------------------------------------------------- v0.25.1 auto reset
def auto_reset_decision(tripped_at, now, trips_30d: int, cfg: dict | None):
    """Pure. cfg = deadman.yaml c4.breaker_auto_reset. Returns 'RESET',
    'REFUSE' or None. A trip clears at the first pass of a LATER session
    date (America/Chicago) if fewer than max_trips_per_30d trips happened in
    the last 30 days; otherwise it stays tripped for the operator (REFUSE,
    emailed once). None when auto reset is off or the trip is today's."""
    c = cfg or {}
    if not c.get("enabled", False) or tripped_at is None:
        return None
    from zoneinfo import ZoneInfo
    ct = ZoneInfo("America/Chicago")
    if tripped_at.astimezone(ct).date() >= now.astimezone(ct).date():
        return None
    if trips_30d >= int(c.get("max_trips_per_30d", 3)):
        return "REFUSE"
    return "RESET"


async def _trip_history() -> tuple:
    """(last trip ts, trips in 30 days, refusal already emailed today)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT max(ts), count(*) FROM journal.audit
               WHERE action='DRAWDOWN_BREAKER_SET' AND new_value='1'
                 AND ts > now() - interval '30 days'""")
        last, n = await cur.fetchone()
        cur = await conn.execute(
            """SELECT 1 FROM journal.audit
               WHERE action='BREAKER_AUTO_RESET_REFUSED'
                 AND (ts AT TIME ZONE 'America/Chicago')::date
                     = (now() AT TIME ZONE 'America/Chicago')::date LIMIT 1""")
        refused_today = (await cur.fetchone()) is not None
    return last, int(n or 0), refused_today


async def maybe_auto_reset(cfg: dict | None, now=None) -> str | None:
    """v0.25.1: called every engine pass. Clears yesterday's breaker trip at
    the first pass of a new session (an unattended month must not stop on
    one bad day), unless trips in 30 days reached the limit."""
    if not (cfg or {}).get("enabled", False) or not await breaker_on():
        return None
    from common.clock import utcnow
    now = now or utcnow()
    last, n, refused_today = await _trip_history()
    decision = auto_reset_decision(last, now, n, cfg)
    if decision == "RESET":
        await set_flag("drawdown_breaker", "0", "C4",
                       f"BREAKER_AUTO_RESET trip {last.isoformat() if last else '?'} "
                       f"trips_30d={n} limit={int(cfg.get('max_trips_per_30d', 3))}")
        await _alert("[breaker] auto reset for the new session",
                     [f"The drawdown breaker tripped on {last.astimezone().date() if last else '?'} and has been "
                      f"cleared for today's session (trip {n} of {int(cfg.get('max_trips_per_30d', 3))} allowed in 30 days).",
                      "Entries resume; the daily cap and per trade risk are unchanged."])
        log.warning("drawdown breaker auto reset", extra=kv(trips_30d=n))
    elif decision == "REFUSE" and not refused_today:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO journal.audit (actor, action, old_value, new_value, detail)
                   VALUES ('C4','BREAKER_AUTO_RESET_REFUSED','1','1',%s)""",
                (f"trips_30d={n} >= limit; operator reset required",))
        await _alert("[breaker] STAYS TRIPPED: 30 day trip limit reached",
                     [f"{n} breaker trips in 30 days reached the limit; entries stay blocked until the "
                      "operator clears drawdown_breaker from the dashboard.",
                      "Exits and the guard keep running."])
        log.error("drawdown breaker stays tripped (limit)", extra=kv(trips_30d=n))
    return decision


async def _alert(subject: str, lines: list[str]) -> None:
    """One ALERT email through the outbox (html + text). Best effort."""
    try:
        from common import mailkit as mk
        html = mk.page("Drawdown breaker", subject.replace("[breaker] ", ""),
                       [mk.section("Detail", mk.bullets(lines))],
                       "c4-exec drawdown breaker", "")
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO journal.outbox (kind, subject, body, html, fact_sheet)
                   VALUES ('ALERT', %s, %s, %s, '{}'::jsonb)""",
                (subject, "\n".join(lines) + "\n", html))
    except Exception as e:                                    # noqa: BLE001
        log.warning("breaker alert failed", extra=kv(error=repr(e)[:120]))

