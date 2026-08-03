"""C7 Watchdog (v0.12.7) — the process that notices dead processes.

Lesson of 2026-07-28: the Spark rebooted at 03:04 and four never-enabled
services (c1-ingestion, c2-dedup, a1-triage, c4-exec) silently stayed down
for five days. journal.health kept showing their last "OK" rows, the daily
emails kept arriving, and the only signal was a vague ingestion note.
Separately, pipeline-nav-snapshot had failed every night since v0.12.2
(missing table) and nothing said so.

This oneshot (fired by c7-watchdog.timer every 5 minutes) checks three
things, entirely from OUTSIDE the services it watches:

  1. Long-running units in config `services:` must be systemd-active
     (and enabled, so they survive the next reboot).
  2. Timer-driven jobs in config `timers:` — the .timer must be active +
     enabled, and the .service's LAST RUN must not have failed
     (Result=exit-code is how the NAV-snapshot bug would have surfaced).
  3. Heartbeats in config `heartbeats:` — journal.health rows that are
     expected to refresh periodically must be younger than max_age_min
     (rth_only entries are only checked during market hours).

Findings become ONE plain-text ALERT email via journal.outbox (C5 mails
it within 5 minutes — the watchdog holds no SMTP credentials, rule 22).
Anti-spam: alert on a CHANGE in the finding set, re-alert every
`realert_hours` while unresolved, and send one recovery email when all
findings clear. State rides in journal.control (watchdog_findings_fp /
watchdog_last_alert_ts). For a service that is DOWN, the watchdog also
overwrites that component's stale journal.health row with DOWN so the
dashboard tells the truth (the dead service cannot object).

Fail-safe notes: every check is read-only except the health/control/outbox
writes; if Postgres itself is down the watchdog can only log and exit
non-zero (single-host system — there is no second channel). The watchdog
never starts, stops, or restarts anything: it reports, the operator acts
(models propose, code disposes; watchdogs neither).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timezone

from common.clock import is_market_hours, utcnow
from common.config import config_path, load_yaml
from common.db import get_pool, close_pool
from common.log import get_logger, kv

log = get_logger("c7.watchdog")

HEALTH_COMPONENT = "watchdog"
SHOW_PROPS = ("LoadState,ActiveState,SubState,UnitFileState,Result,"
              "InactiveEnterTimestamp")
FP_KEY = "watchdog_findings_fp"
TS_KEY = "watchdog_last_alert_ts"

# v0.12.10: systemd states that mean "mid-transition", not "down". The
# v0.12.9 deploy restart raced a scheduled pass and emailed a false
# SERVICE_DOWN ("deactivating") followed by RECOVERED minutes later. A unit
# seen in one of these states produces NO finding this pass: a normal
# restart is active next pass; a crashed one shows failed/inactive next
# pass (5 min — well inside every heartbeat limit); and a service wedged
# mid-activation still trips its HEARTBEAT_STALE check. Nothing is lost
# except the false alarm.
TRANSIENT_STATES = ("activating", "deactivating", "reloading", "refreshing")


# ---------------------------------------------------------------------------
# systemd introspection (read-only; no sudo required for `systemctl show`)
# ---------------------------------------------------------------------------

def systemctl_show(unit: str, runner=subprocess.run) -> dict:
    """`systemctl show <unit> -p <props>` parsed into a dict. Missing units
    still answer (LoadState=not-found); errors return an empty dict so one
    bad probe never kills the pass."""
    try:
        proc = runner(["systemctl", "show", unit, "-p", SHOW_PROPS],
                      capture_output=True, text=True, timeout=10)
    except Exception as e:                                    # noqa: BLE001
        log.error("systemctl probe failed", extra=kv(unit=unit,
                                                     error=repr(e)[:120]))
        return {}
    props = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v.strip()
    return props


def gather_units(cfg: dict, runner=subprocess.run) -> dict:
    """Probe every unit the config mentions. Keys: 'c4-exec.service',
    'c5-mailer.timer', ..."""
    units = {}
    for name in (cfg.get("services") or {}):
        units[f"{name}.service"] = systemctl_show(f"{name}.service", runner)
    for name in (cfg.get("timers") or []):
        units[f"{name}.timer"] = systemctl_show(f"{name}.timer", runner)
        units[f"{name}.service"] = systemctl_show(f"{name}.service", runner)
    return units


# ---------------------------------------------------------------------------
# pure evaluation (unit-tested without a DB or systemd)
# ---------------------------------------------------------------------------

def evaluate(cfg: dict, units: dict, ages_min: dict,
             in_session: bool) -> list[dict]:
    """Turn probes into findings. Each finding:
    {severity: CRITICAL|WARNING, code, unit, detail}. Deterministic order."""
    findings = []

    def add(severity: str, code: str, unit: str, detail: str) -> None:
        findings.append({"severity": severity, "code": code,
                         "unit": unit, "detail": detail})

    for name, opts in sorted((cfg.get("services") or {}).items()):
        opts = opts or {}
        info = units.get(f"{name}.service", {})
        desc = opts.get("desc", "")
        if not info or info.get("LoadState") == "not-found":
            add("WARNING", "UNIT_NOT_FOUND", name,
                "unit file not found on this machine — remove it from "
                "config/watchdog.yaml or install the unit")
            continue
        state = info.get("ActiveState")
        if state in TRANSIENT_STATES:
            continue                       # v0.12.10: recheck next pass
        if state != "active":
            since = info.get("InactiveEnterTimestamp") or "unknown time"
            add("CRITICAL", "SERVICE_DOWN", name,
                f"{state or '?'} since {since}"
                + (f" — {desc}" if desc else ""))
        if info.get("UnitFileState") not in ("enabled", "static",
                                             "enabled-runtime"):
            add("WARNING", "NOT_ENABLED", name,
                "will NOT restart after a reboot "
                f"(unit file state: {info.get('UnitFileState') or 'unknown'}) "
                "— fix: sudo systemctl enable " + name)

    for name in sorted(cfg.get("timers") or []):
        tinfo = units.get(f"{name}.timer", {})
        sinfo = units.get(f"{name}.service", {})
        if not tinfo or tinfo.get("LoadState") == "not-found":
            add("WARNING", "UNIT_NOT_FOUND", f"{name}.timer",
                "timer not found on this machine — remove it from "
                "config/watchdog.yaml or install the unit")
            continue
        tstate = tinfo.get("ActiveState")
        if tstate in TRANSIENT_STATES:
            pass                           # v0.12.10: recheck next pass
        elif tstate != "active":
            add("CRITICAL", "TIMER_DOWN", f"{name}.timer",
                f"timer is {tstate or '?'} — its job never "
                "fires — fix: sudo systemctl enable --now " + name + ".timer")
        elif tinfo.get("UnitFileState") not in ("enabled", "static",
                                                "enabled-runtime"):
            add("WARNING", "NOT_ENABLED", f"{name}.timer",
                "will NOT restart after a reboot — fix: "
                "sudo systemctl enable " + name + ".timer")
        if sinfo.get("Result") not in ("success", "", None):
            add("CRITICAL", "LAST_RUN_FAILED", name,
                f"last run failed (Result={sinfo.get('Result')}) — see: "
                "journalctl -u " + name + " -n 40")

    for component, opts in sorted((cfg.get("heartbeats") or {}).items()):
        opts = opts or {}
        if opts.get("rth_only") and not in_session:
            continue
        age = ages_min.get(component)
        if age is None:
            continue                     # never wrote — unit checks cover it
        max_age = float(opts.get("max_age_min", 60))
        if age > max_age:
            add("CRITICAL", "HEARTBEAT_STALE", component,
                f"journal.health not updated for {age:.0f} min "
                f"(limit {max_age:.0f}) — the process may be up but wedged")

    return findings


def fingerprint(findings: list[dict]) -> str:
    """Stable identity of the finding SET (codes+units only — ages and
    timestamps churn every pass and must not retrigger alerts)."""
    key = sorted((f["code"], f["unit"]) for f in findings)
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16] if key else ""


def should_alert(findings: list[dict], prev_fp: str, last_alert: datetime | None,
                 now: datetime, realert_hours: float) -> str | None:
    """Returns 'NEW' | 'REPEAT' | 'RECOVERED' | None."""
    fp = fingerprint(findings)
    if findings:
        if fp != prev_fp:
            return "NEW"
        if last_alert is None or \
                (now - last_alert).total_seconds() >= realert_hours * 3600:
            return "REPEAT"
        return None
    return "RECOVERED" if prev_fp else None


def render(findings: list[dict], mode: str, realert_hours: float) -> tuple[str, str]:
    """(subject, body) — plain text, worst first, one line per finding."""
    if mode == "RECOVERED":
        return ("[watchdog] RECOVERED — all monitored units healthy",
                "All previously reported problems have cleared.\n"
                "No action needed.\n")
    crit = [f for f in findings if f["severity"] == "CRITICAL"]
    warn = [f for f in findings if f["severity"] == "WARNING"]
    worst = crit[0]["unit"] if crit else warn[0]["unit"]
    prefix = "REPEAT: " if mode == "REPEAT" else ""
    subject = (f"[watchdog] {prefix}{len(crit)} critical / {len(warn)} warning"
               f" — worst: {worst}")
    lines = ["The pipeline watchdog found problems.", ""]
    for f in crit:
        lines.append(f"CRITICAL  {f['unit']}: [{f['code']}] {f['detail']}")
    for f in warn:
        lines.append(f"WARNING   {f['unit']}: [{f['code']}] {f['detail']}")
    lines += ["",
              "First move: systemctl status <unit> ; cold-start order is in "
              "ops/RUNBOOK.md section 6.",
              f"This email repeats every {realert_hours:g}h until resolved, "
              "and once more on recovery.",
              "Sent by c7-watchdog (fires every 5 minutes, holds no "
              "credentials, changes nothing)."]
    return subject, "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# DB side effects
# ---------------------------------------------------------------------------

async def _heartbeat_ages(now: datetime) -> dict[str, float]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT component, updated_ts FROM journal.health")
        rows = await cur.fetchall()
    ages = {}
    for component, ts in rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ages[component] = (now - ts).total_seconds() / 60.0
    return ages


async def _set_health(component: str, status: str, detail: str) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO journal.health (component, status, detail, updated_ts)
               VALUES (%s,%s,%s, now())
               ON CONFLICT (component) DO UPDATE
               SET status=EXCLUDED.status, detail=EXCLUDED.detail,
                   updated_ts=EXCLUDED.updated_ts""",
            (component, status, detail[:500]))


async def _get_control(key: str) -> str | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT value FROM journal.control WHERE key=%s", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def _set_control(key: str, value: str) -> None:
    """Plain upsert — no audit row; watchdog bookkeeping is not an
    operational control change."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO journal.control (key, value, updated_ts)
               VALUES (%s,%s,now())
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                                               updated_ts=now()""",
            (key, value))


async def _queue_alert(subject: str, body: str, findings: list[dict]) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO journal.outbox (kind, subject, body, fact_sheet)
               VALUES ('ALERT', %s, %s, %s::jsonb)""",
            (subject, body, json.dumps({"findings": findings})))


# ---------------------------------------------------------------------------
# one pass
# ---------------------------------------------------------------------------

async def run_pass(cfg: dict, now: datetime | None = None,
                   runner=subprocess.run) -> dict:
    """One watchdog pass. Returns a summary dict (tested/logged)."""
    now = now or utcnow()
    in_session = is_market_hours(now)
    units = gather_units(cfg, runner)
    ages = await _heartbeat_ages(now)
    findings = evaluate(cfg, units, ages, in_session)

    # Make the dashboard truthful: a DOWN service's stale health row
    # becomes DOWN (only for components mapped in config).
    for f in findings:
        if f["code"] == "SERVICE_DOWN":
            comp = ((cfg.get("services") or {}).get(f["unit"]) or {}).get("health")
            if comp:
                await _set_health(comp, "DOWN",
                                  f"watchdog: {f['unit']} {f['detail'][:200]}")

    prev_fp = await _get_control(FP_KEY) or ""
    raw_ts = await _get_control(TS_KEY)
    last_alert = None
    if raw_ts:
        try:
            last_alert = datetime.fromisoformat(raw_ts)
        except ValueError:
            last_alert = None
    realert_hours = float(cfg.get("realert_hours", 6))

    mode = should_alert(findings, prev_fp, last_alert, now, realert_hours)
    if mode:
        subject, body = render(findings, mode, realert_hours)
        await _queue_alert(subject, body, findings)
        await _set_control(FP_KEY, fingerprint(findings))
        await _set_control(TS_KEY, now.isoformat())
        log.warning("alert queued", extra=kv(mode=mode, subject=subject[:100]))
    elif not findings and not prev_fp:
        pass                                              # steady-state quiet

    crit = sum(1 for f in findings if f["severity"] == "CRITICAL")
    warn = len(findings) - crit
    if findings:
        await _set_health(HEALTH_COMPONENT, "DEGRADED",
                          f"{crit} critical / {warn} warning findings")
    else:
        await _set_health(HEALTH_COMPONENT, "OK",
                          f"all clear ({len(units)} units checked)")
    return {"findings": len(findings), "critical": crit, "warning": warn,
            "alert": mode or "none"}


async def main() -> None:
    cfg = load_yaml(config_path("watchdog.yaml"))
    try:
        summary = await run_pass(cfg)
        log.info("watchdog pass", extra=kv(**summary))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
