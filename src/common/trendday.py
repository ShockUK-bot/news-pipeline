"""Trend day mode (v0.31.0, 2026-11-07).

A deterministic, session scoped switch. From 10:00 ET the scanner checks the
index ETFs: if any is down `min_drop_pct` or more from its previous close AND
below its own 09:45 ET price, the day is a DOWN trend day. The flag lives in
journal.control (`trend_day` = "down@YYYY-MM-DD") and is read by C3, A3 and
C10. It never clears during the session; a new date makes the old value
inert. Consumers:

  C3  news shorts lane on; new LONG passes become VETO TREND_DAY_LONG;
      scanner structure checks relaxed for with-move shorts.
  A3  LONG entries vetoed (defense in depth); scanner concurrency, notional,
      short heat and gross short caps from the trend_day block.
  C10 per scan and per hour caps raised for down movers.

Pure functions first (tested without a DB), then the thin async layer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from common.config import config_path, load_yaml
from common.db import get_pool
from common.log import get_logger, kv

log = get_logger("common.trendday")
ET = ZoneInfo("America/New_York")
CONTROL_KEY = "trend_day"


def load_cfg() -> dict:
    try:
        return (load_yaml(config_path("trend_day.yaml")) or {}).get("trend_day") or {}
    except Exception as e:                                        # noqa: BLE001
        log.warning("trend_day.yaml unavailable; mode off", extra=kv(error=repr(e)[:120]))
        return {}


# ---------------------------------------------------------------- pure rules

def detect(prev_close: Optional[float], px_0945: Optional[float],
           last: Optional[float], cfg: dict) -> bool:
    """True when `last` is at or below prev_close * (1 - min_drop_pct) and
    (if required) below the 09:45 price. Missing inputs never trigger."""
    if not cfg.get("enabled", False):
        return False
    if not prev_close or not last or prev_close <= 0:
        return False
    drop = last / prev_close - 1.0
    if drop > -float(cfg.get("min_drop_pct", 0.02)):
        return False
    if cfg.get("require_below_0945", True):
        if not px_0945 or last >= px_0945:
            return False
    return True


def flag_value(day_et: str, direction: str = "down") -> str:
    return f"{direction}@{day_et}"


def parse_flag(value: Optional[str], today_et: str) -> Optional[str]:
    """'down' when the stored flag is for today, else None."""
    if not value or "@" not in str(value):
        return None
    direction, day = str(value).split("@", 1)
    return direction if day == today_et and direction in ("down", "up") else None


def past_check_time(now_et: datetime, cfg: dict) -> bool:
    return now_et.strftime("%H:%M") >= str(cfg.get("check_from_et", "10:00"))


def scanner_caps(scanner_cfg: dict, cfg: dict) -> dict:
    """C10 emission caps for down movers on a down trend day."""
    sc = (cfg.get("scanner") or {})
    out = dict(scanner_cfg)
    for k in ("max_per_scan", "max_per_hour", "max_concurrent_positions"):
        if sc.get(k) is not None:
            out[k] = int(sc[k])
    return out


def gate_scanner_cfg(scfg: dict, direction: str, cfg: dict) -> dict:
    """C3 scanner rule config for a with-move SHORT on a down trend day:
    the structure checks (VWAP side, range position) are what vetoed PLTR,
    SPCX and MSTR on the 11:00 bounce of 2026-11-05."""
    sc = (cfg.get("scanner") or {})
    if direction != "down" or not sc.get("relax_structure_for_shorts", True):
        return scfg
    out = dict(scfg)
    out["require_above_vwap"] = False
    out["range30_min_pos"] = 0.0
    return out


def risk_overrides(capital_cfg: dict, scanner_cfg: dict, shorting_cfg: dict,
                   cfg: dict) -> tuple[dict, dict, dict]:
    """(capital, scanner, shorting) config copies with the trend day caps."""
    cap = dict(capital_cfg); sc = dict(scanner_cfg); sh = dict(shorting_cfg)
    t_sc = cfg.get("scanner") or {}
    t_sh = cfg.get("shorting") or {}
    t_cap = cfg.get("capital") or {}
    if t_sc.get("max_concurrent_positions") is not None:
        sc["max_concurrent_positions"] = int(t_sc["max_concurrent_positions"])
    if t_sc.get("max_position_notional_pct") is not None:
        sc["max_position_notional_pct"] = float(t_sc["max_position_notional_pct"])
    for k in ("max_gross_short_notional_pct", "max_short_heat_pct"):
        if t_sh.get(k) is not None:
            sh[k] = float(t_sh[k])
    if t_cap.get("heat_split_short_pct") is not None:
        hs = dict(cap.get("heat_split") or {})
        hs["SHORT"] = float(t_cap["heat_split_short_pct"])
        cap["heat_split"] = hs
    if t_cap.get("max_portfolio_heat_pct") is not None:
        cap["max_portfolio_heat_pct"] = float(t_cap["max_portfolio_heat_pct"])
    return cap, sc, sh


# ------------------------------------------------------------- async layer

async def active(now: Optional[datetime] = None) -> Optional[str]:
    """'down' when today's flag is set, else None. One cheap control read."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ET).strftime("%Y-%m-%d")
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT value FROM journal.control WHERE key=%s", (CONTROL_KEY,))
        row = await cur.fetchone()
    return parse_flag(row[0] if row else None, today)


async def _set(value: str, actor: str, detail: str, numbers: dict) -> None:
    from common.journal import write_decision
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT value FROM journal.control WHERE key=%s", (CONTROL_KEY,))
            row = await cur.fetchone()
            await conn.execute(
                """INSERT INTO journal.control (key, value, updated_ts) VALUES (%s,%s,now())
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_ts=now()""",
                (CONTROL_KEY, value))
            await conn.execute(
                """INSERT INTO journal.audit (actor, action, old_value, new_value, detail)
                   VALUES (%s,%s,%s,%s,%s)""",
                (actor, "TREND_DAY_SET", row[0] if row else None, value, detail[:300]))
            await write_decision(signal_id=f"trend-day-{value}", stage="SYSTEM", agent=actor,
                                 action="TREND_DAY", payload=numbers, reason=detail[:300], conn=conn)


async def evaluate_and_set(md, now: datetime, cfg: dict, actor: str = "C10") -> Optional[str]:
    """Called every scanner cycle. Returns the active direction (or None).
    Sets the flag the first time the rule fires today; never unsets it."""
    if not cfg.get("enabled", False):
        return None
    now_et = now.astimezone(ET)
    today = now_et.strftime("%Y-%m-%d")
    current = await active(now)
    if current:
        return current
    if now_et.weekday() >= 5 or not past_check_time(now_et, cfg):
        return None
    open_utc = now_et.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(timezone.utc)
    for sym in cfg.get("symbols") or ["QQQ", "SPY"]:
        try:
            prev = await md.prev_close(sym)
            quote = await md.snapshot(sym)
            bars = await md.minute_bars(sym, open_utc, now)
            b0945 = next((b for b in bars if b["ts"].astimezone(ET).strftime("%H:%M") == "09:45"), None)
            px0945 = float(b0945["open"]) if b0945 else None
            if detect(prev, px0945, quote.price, cfg):
                numbers = {"symbol": sym, "prev_close": prev, "px_0945": px0945, "last": quote.price,
                           "drop_pct": round(quote.price / prev - 1, 5), "checked_et": now_et.strftime("%H:%M")}
                value = flag_value(today, str(cfg.get("direction", "down")))
                await _set(value, actor, f"trend day: {sym} {numbers['drop_pct']:+.2%} vs prev close at "
                                         f"{numbers['checked_et']} ET, below its 09:45 price", numbers)
                log.warning("TREND DAY set", extra=kv(**numbers))
                return "down"
        except Exception as e:                                    # noqa: BLE001
            log.warning("trend day check failed", extra=kv(symbol=sym, error=repr(e)[:150]))
    return None
