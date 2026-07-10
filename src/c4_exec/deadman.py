"""Dead-man switch monitor (phase4-design-v1_0 D4).

Reads journal.health heartbeat timestamps; applies the ladder from
config/deadman.yaml. Escalation is ALERT -> BLOCK_ENTRIES -> (marketdata
only) exit-engine suspend. NEVER auto-flatten: catastrophe stops are
broker-resident precisely so that a dead pipeline leaves protected
positions, and a panicked robot selling into an outage is worse than one
that stands still.

Ownership rule: the monitor only CLEARS blocks it set itself (control key
deadman_block='1' marks ownership) — an operator's manual block_entries is
never unwound by code. Runs inside C4's monitor task; RTH-only for
escalations, ALERT-only off-hours.
"""
from __future__ import annotations

from datetime import datetime, timezone

from common.db import get_pool
from common.log import get_logger, kv
from c1_ingestion.heartbeat import set_health

from .flags import get_flag, set_flag

log = get_logger("monitor.deadman")

COMPONENT_MAP = {"ingestion": "ingestion", "marketdata": "marketdata",
                 "triage": "triage_model", "analyst": "analyst_model",
                 "gate": "gate"}


async def heartbeat_ages(now: datetime) -> dict[str, float]:
    """Minutes since each component's last health update."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT component, updated_ts FROM journal.health")
        rows = await cur.fetchall()
    ages = {}
    for component, ts in rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ages[component] = (now - ts).total_seconds() / 60.0
    return ages


async def check(cfg: dict, now: datetime, in_session: bool) -> dict:
    """One monitor pass. Returns the actions taken (for tests/logging)."""
    ages = await heartbeat_ages(now)
    actions = {"alerts": [], "block": False, "unblock": False,
               "exit_suspend": False, "exit_resume": False}
    want_block = False
    want_exit_suspend = False

    for name, thresholds in cfg["components"].items():
        component = COMPONENT_MAP.get(name, name)
        age = ages.get(component)
        if age is None:
            continue                      # component never started — cold start
        if age > thresholds["alert_min"]:
            actions["alerts"].append((component, round(age, 1)))
        if in_session and "block_entries_min" in thresholds \
                and age > thresholds["block_entries_min"]:
            want_block = True
        if in_session and "exit_engine_suspend_min" in thresholds \
                and age > thresholds["exit_engine_suspend_min"]:
            want_exit_suspend = True

    deadman_owns = await get_flag("deadman_block") == "1"
    blocked = await get_flag("block_entries") == "1"

    if want_block and not blocked:
        await set_flag("block_entries", "1", "DEADMAN",
                       f"heartbeat stale: {actions['alerts']}")
        await set_flag("deadman_block", "1", "DEADMAN")
        actions["block"] = True
        log.warning("dead-man BLOCK_ENTRIES", extra=kv(alerts=actions["alerts"]))
    elif not want_block and blocked and deadman_owns:
        await set_flag("block_entries", "0", "DEADMAN", "heartbeats recovered")
        await set_flag("deadman_block", "0", "DEADMAN")
        actions["unblock"] = True
        log.info("dead-man unblock: heartbeats recovered")

    exit_suspended = await get_flag("exit_engine_suspended") == "1"
    if want_exit_suspend and not exit_suspended:
        await set_flag("exit_engine_suspended", "1", "DEADMAN",
                       "marketdata stale >suspend threshold: catastrophe "
                       "stops are sole protection")
        actions["exit_suspend"] = True
        log.error("EXIT ENGINE SUSPENDED — catastrophe stops sole protection")
    elif not want_exit_suspend and exit_suspended:
        await set_flag("exit_engine_suspended", "0", "DEADMAN",
                       "marketdata recovered")
        actions["exit_resume"] = True

    for component, age in actions["alerts"]:
        await set_health("deadman", "DEGRADED",
                         f"stale: {component} {age}min")
    if not actions["alerts"]:
        await set_health("deadman", "OK", "all heartbeats fresh")
    return actions

