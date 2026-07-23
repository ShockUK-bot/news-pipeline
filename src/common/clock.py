"""UTC discipline (baseline §11.5). Every timestamp the pipeline creates or
parses goes through this module. ET conversion happens only in market-logic
code, and only via market_hours_now() here — never ad hoc.

Market-hours here is deliberately coarse (gap-threshold selection only):
weekday 9:30–16:00 ET. The exchange-calendar library arrives with C3/C4 where
holiday precision is load-bearing; a gap alert on a holiday is a false positive
we tolerate in Phase 1, not a trading error.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    """The only clock the pipeline reads."""
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None = None) -> str:
    """ISO-8601 UTC with milliseconds — the contract timestamp format (spec §3)."""
    dt = dt or utcnow()
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected: all timestamps must be aware")
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(raw: str | int | float | datetime) -> datetime:
    """Parse a source timestamp into an aware UTC datetime.

    Raises ValueError for anything unparseable or naive — callers route those
    items to quarantine with BAD_TIMESTAMP (v0.4: quarantine, never drop).
    """
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raise ValueError(f"naive datetime: {raw!r}")
        return raw.astimezone(timezone.utc)
    if isinstance(raw, (int, float)):
        # epoch seconds or milliseconds; sanity-bounded to 2000–2100
        val = float(raw)
        if val > 1e12:
            val /= 1000.0
        if not 946_684_800 <= val <= 4_102_444_800:
            raise ValueError(f"epoch out of range: {raw!r}")
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise ValueError("empty timestamp")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # v0.5.9: RFC-822/RSS-style dates ("Fri, 17 Jul 2026 22:30 GMT").
            # Some feeds (globenewswire) omit the seconds, which fromisoformat
            # cannot read; the email date parser handles the whole family.
            try:
                dt = parsedate_to_datetime(s)
            except (ValueError, TypeError) as e:
                raise ValueError(f"unparseable timestamp: {raw!r}") from e
        if dt.tzinfo is None:
            raise ValueError(f"naive timestamp: {raw!r}")
        return dt.astimezone(timezone.utc)
    raise ValueError(f"unsupported timestamp type: {type(raw)}")


def is_market_hours(dt: datetime | None = None) -> bool:
    """Coarse RTH check for gap-threshold selection (see module docstring)."""
    et = (dt or utcnow()).astimezone(_ET)
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return (9 * 60 + 30) <= minutes < (16 * 60)


def session_open(dt: datetime | None = None) -> datetime | None:
    """UTC timestamp of the 9:30 ET session open for dt's ET calendar date,
    or None on weekends. Same coarseness contract as is_market_hours (no
    holiday calendar); consumers treat 'no bars' as the ground truth."""
    et = (dt or utcnow()).astimezone(_ET)
    if et.weekday() >= 5:
        return None
    return et.replace(hour=9, minute=30, second=0,
                      microsecond=0).astimezone(timezone.utc)

