"""Burst rules over a SymbolBook. Pure functions; the service supplies the clock.

An Event is a measurement, not a trade. `momentum` events carry the burst
direction; the service also journals a `fade` row (opposite direction) for the
same burst so both hypotheses get scored from one detection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .book import SymbolBook

CT = ZoneInfo("America/Chicago")


@dataclass
class Event:
    symbol: str
    ts: float
    rule: str
    direction: int           # +1 long burst, -1 short burst
    price: float
    ret_60s: Optional[float]
    ret_120s: Optional[float]
    vol_mult: Optional[float]
    new_extreme: bool
    detail: dict = field(default_factory=dict)


def in_session(now: float, cfg: dict) -> bool:
    """Weekday and inside [session_start_ct, session_end_ct]. Holidays produce
    no trades, so they fall out naturally."""
    dt = datetime.fromtimestamp(now, CT)
    if dt.weekday() >= 5:
        return False
    hhmm = dt.strftime("%H:%M")
    return str(cfg.get("session_start_ct", "08:36")) <= hhmm <= str(cfg.get("session_end_ct", "14:55"))


def session_open_ts(now: float) -> float:
    dt = datetime.fromtimestamp(now, CT).replace(hour=8, minute=30, second=0, microsecond=0)
    return dt.timestamp()


def evaluate(book: SymbolBook, now: float, cfg: dict,
             last_fire: dict[str, float]) -> Optional[Event]:
    """One symbol, one pass. Returns a momentum Event or None. `last_fire`
    is the per-symbol cooldown map (mutated on fire)."""
    if book.last_price is None or book.last_ts is None:
        return None
    if now - book.last_ts > float(cfg.get("stale_secs", 30)):
        return None                                  # no prints lately
    cd = float(cfg.get("cooldown_secs", 900))
    if now - last_fire.get(book.symbol, -1e12) < cd:
        return None
    r60 = book.ret(now, 60)
    r120 = book.ret(now, 120)
    if r60 is None and r120 is None:
        return None
    thr60 = float(cfg.get("ret_60s", 0.004))
    thr120 = float(cfg.get("ret_120s", 0.006))
    direction = 0
    if r60 is not None and abs(r60) >= thr60:
        direction = 1 if r60 > 0 else -1
    elif r120 is not None and abs(r120) >= thr120:
        direction = 1 if r120 > 0 else -1
    if direction == 0:
        return None
    vm = book.vol_mult(now, float(cfg.get("vol_window_secs", 60)),
                       float(cfg.get("baseline_secs", 1800)),
                       int(cfg.get("min_baseline_buckets", 24)),
                       baseline_from_ts=session_open_ts(now) if cfg.get("baseline_rth_only", True) else None)
    if vm is None or vm < float(cfg.get("vol_mult", 4.0)):
        return None
    hi, lo = book.extreme(now, float(cfg.get("extreme_secs", 1800)))
    new_ext = bool(hi is not None and lo is not None and
                   ((direction > 0 and book.last_price >= hi) or
                    (direction < 0 and book.last_price <= lo)))
    if cfg.get("require_extreme", True) and not new_ext:
        return None
    last_fire[book.symbol] = now
    return Event(symbol=book.symbol, ts=now, rule="momentum", direction=direction,
                 price=book.last_price, ret_60s=r60, ret_120s=r120, vol_mult=vm,
                 new_extreme=new_ext,
                 detail={"thr60": thr60, "thr120": thr120, "vol_mult_min": float(cfg.get("vol_mult", 4.0))})
