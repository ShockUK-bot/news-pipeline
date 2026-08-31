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

v0.14.4, both changes forced by the 2026-08-27 incident:

* The health row used to be written INSIDE the alert loop, once per stale
  component, each write overwriting the last. Only the final component in
  the list was ever visible, so a genuinely dead ingestion could hide behind
  a stale gate. It is now one row naming every stale component, worst first.
* `critical_min` (optional, per component) makes the row DOWN rather than
  DEGRADED once an outage is long rather than momentary. triage was stale
  for 85 hours and rendered exactly like a five-minute blip, so four
  consecutive morning emails carried the same yellow line and nobody moved.
  Duration is information; the ladder now carries it.

Neither change adds blocking behaviour. Escalation to BLOCK_ENTRIES is still
driven solely by `block_entries_min`, which only ingestion and marketdata
declare, exactly as before.
"""
from __future__ import annotations

from datetime import datetime, timezone

from common.db import get_pool
from common.log import get_logger, kv
from c1_ingestion.heartbeat import set_health

from .flags import get_flag, set_flag

log = get_logger("monitor.deadman")


def _span(minutes: float) -> str:
    """Human units. '5102.3min' is a number you skim past; '85.0h' is not."""
    if minutes < 180:
        return f"{minutes:.1f}min"
    if minutes < 2880:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f} days"

# Maps deadman.yaml component names -> journal.health component names. A1/A2/C3
# write their heartbeats under 'triage'/'analyst'/'gate'. (Fixed 2026-07-20 in
# v0.11.7: the old 'triage_model'/'analyst_model' names matched no health row,
# so the dead-man silently skipped triage and analyst — never monitoring them.)
COMPONENT_MAP = {"ingestion": "ingestion", "marketdata": "marketdata",
                 "triage": "triage", "analyst": "analyst",
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
    stale: list[dict] = []          # local: component, age, critical
    want_block = False
    want_exit_suspend = False

    for name, thresholds in cfg["components"].items():
        component = COMPONENT_MAP.get(name, name)
        age = ages.get(component)
        if age is None:
            continue                      # component never started — cold start
        # marketdata is only expected fresh during RTH — no live quotes flow
        # off-hours, so its staleness then is normal and not actionable. Skip
        # it entirely out of session (v0.11.8); every other component runs 24/7
        # and should still alert. (Escalations were already in_session-gated.)
        if name == "marketdata" and not in_session:
            continue
        if age > thresholds["alert_min"]:
            actions["alerts"].append((component, round(age, 1)))
            crit_min = thresholds.get("critical_min")
            stale.append({"component": component, "age": age,
                          "critical": crit_min is not None
                          and age > float(crit_min)})
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

    if stale:
        # Worst first, and one row rather than one write per component. NOTE:
        # `actions` deliberately keeps its original keys — severity is local,
        # so existing callers and tests see the same shape they always did.
        stale.sort(key=lambda s: s["age"], reverse=True)
        status = "DOWN" if any(s["critical"] for s in stale) else "DEGRADED"
        listed = ", ".join(f"{s['component']} {_span(s['age'])}"
                           for s in stale)
        await set_health("deadman", status, f"{len(stale)} stale: {listed}")
    else:
        await set_health("deadman", "OK", "all heartbeats fresh")
    return actions

