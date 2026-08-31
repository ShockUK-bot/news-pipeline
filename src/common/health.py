"""Shared freshness expectations for journal.health rows (v0.14.4).

ONE source of truth. `config/watchdog.yaml` declares, per component, how old
its health row is allowed to get before something is wrong. C7 turns a breach
into an alert email; A8 puts it at the top of the morning briefing. Before
this module each of those had its own private idea of "stale", and between
them they covered `exec`, `deadman`, `marketdata`, `mailer`, `backup` and
`earnings` — not `triage`, `dedup`, `risk`, `gate`, `analyst` or `guard`.

That gap is the 2026-08-27 incident. a1-triage hung during a reboot, systemd
reported `active (running)` for 85 hours because a hung process never exits,
C7 checked systemd state and agreed, and the `triage` health row sat at OK
with a timestamp from the reboot. The one component that noticed was the
dead-man, whose only escalation for triage is ALERT, so four consecutive
morning emails carried the same yellow line and a full trading session was
lost to a starved pipeline.

Rules encoded here:

* A service is checked for freshness only when it declares BOTH a `health:`
  component and a `max_age_min:`. A windowed job (c10-scanner writes only
  between 09:50 and 15:15 ET) declares no limit and is covered by its unit
  check alone. Silence about a limit is deliberate, never an oversight.
* A component that has NEVER written a row is not stale, it is absent, and
  the unit checks own that case.
* `ingestion:<source>` rows are written by an event (GapMonitor opens and
  closes gap rows), not by a clock, so they are exempt from the orphan
  warning. They are still deleted once genuinely ancient.
"""
from __future__ import annotations

# Rows written by an event rather than a clock. A healthy Alpaca websocket
# that has stayed connected for a week has a week-old row, correctly.
DYNAMIC_PREFIXES = ("ingestion:",)


def is_dynamic(component: str) -> bool:
    """True for rows whose age carries no information about liveness."""
    return component.startswith(DYNAMIC_PREFIXES)


def freshness_limits(cfg: dict) -> dict[str, dict]:
    """Build component -> {max_age_min, rth_only, unit, desc} from both halves
    of watchdog.yaml.

    `services:` entries contribute when they name a health component AND a
    max_age_min. The standalone `heartbeats:` block contributes components
    that belong to no single unit (deadman lives inside c4-exec; marketdata
    is written by whichever source is connected). Where both describe the
    same component the heartbeats block wins, but it inherits the unit name
    so an alert can still say which service to restart.
    """
    limits: dict[str, dict] = {}

    for unit, opts in (cfg.get("services") or {}).items():
        opts = opts or {}
        comp = opts.get("health")
        limit = opts.get("max_age_min")
        if not comp or limit in (None, ""):
            continue
        limits[comp] = {"max_age_min": float(limit),
                        "rth_only": bool(opts.get("rth_only")),
                        "unit": unit,
                        "desc": opts.get("desc", "")}

    for comp, opts in (cfg.get("heartbeats") or {}).items():
        opts = opts or {}
        prior = limits.get(comp, {})
        limits[comp] = {"max_age_min": float(opts.get("max_age_min", 60)),
                        "rth_only": bool(opts.get("rth_only")),
                        "unit": opts.get("unit") or prior.get("unit", ""),
                        "desc": opts.get("desc") or prior.get("desc", "")}

    return limits


def stale_components(limits: dict[str, dict], ages_min: dict[str, float],
                     in_session: bool) -> list[dict]:
    """Components past their limit, worst first.

    Each entry: {component, age_min, max_age_min, unit, desc}. An rth_only
    component is skipped outside market hours (no live quotes flow off-hours,
    so marketdata staleness then is normal). A component with no row at all
    is skipped: absent is not stale.
    """
    out = []
    for comp, info in limits.items():
        if info.get("rth_only") and not in_session:
            continue
        age = ages_min.get(comp)
        if age is None:
            continue
        if age > info["max_age_min"]:
            out.append({"component": comp,
                        "age_min": age,
                        "max_age_min": info["max_age_min"],
                        "unit": info.get("unit", ""),
                        "desc": info.get("desc", "")})
    out.sort(key=lambda r: r["age_min"], reverse=True)
    return out


def orphan_rows(cfg: dict, ages_min: dict[str, float],
                warn_after_days: float,
                delete_after_days: float) -> tuple[list[str], list[str]]:
    """(to_warn, to_delete) for health rows no config claims.

    The rule follows the lesson of the incident: a row that LIES is worse
    than no row at all. `ingestion:testsource` sat at OK for 47 days after
    the source was removed, indistinguishable from a healthy component.

    to_delete — any unclaimed row past `delete_after_days`, dynamic rows
        included. Nothing has written it in a month, so nothing alive is
        behind it. If something alive is behind it after all, it writes the
        row again on its next event and no information is lost.
    to_warn — unclaimed NON-dynamic rows past `warn_after_days`, minus
        anything being deleted this pass. Dynamic `ingestion:*` rows are
        never warned about: a websocket that has held its connection for two
        weeks correctly has a two-week-old row, and warning about it every
        day would recreate the yellow-line-you-stop-reading failure that let
        the 2026-08-27 outage run for four mornings.
    """
    claimed = claimed_components(cfg)
    unclaimed = {c: age for c, age in ages_min.items() if c not in claimed}

    to_delete = sorted(c for c, age in unclaimed.items()
                       if delete_after_days > 0
                       and age > delete_after_days * 1440)
    doomed = set(to_delete)
    to_warn = sorted(c for c, age in unclaimed.items()
                     if warn_after_days > 0
                     and age > warn_after_days * 1440
                     and c not in doomed
                     and not is_dynamic(c))
    return to_warn, to_delete


def claimed_components(cfg: dict) -> set[str]:
    """Every component any part of watchdog.yaml mentions, whether or not it
    carries a freshness limit. Anything outside this set with an old row is an
    orphan: a component that was renamed or removed and whose row nothing
    writes to any more, so it reads OK forever (`ingestion:testsource` sat at
    OK for 47 days before v0.14.4 noticed)."""
    claimed = set()
    for opts in (cfg.get("services") or {}).values():
        comp = (opts or {}).get("health")
        if comp:
            claimed.add(comp)
    claimed.update((cfg.get("heartbeats") or {}).keys())
    claimed.update(cfg.get("never_orphan") or [])
    return claimed
