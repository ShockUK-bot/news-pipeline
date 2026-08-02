"""v0.12.8 — vocabulary-union guards.

Root cause of the a6-nightly outage (2026-07-31, found 2026-08-02): a
migration rebuilt a CHECK constraint from a stale value list, silently
deleting values an earlier migration had added. These tests make that
mistake un-shippable:

  1. For each vocabulary constraint, the NEWEST migration that rebuilds it
     must contain the full historical union (a shrink fails the suite).
  2. Every event_type literal the code writes must be present in the
     newest position_events list (new code without a migration fails too).

Pure filesystem tests — no DB, no imports of service code.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "schema" / "migrations"
SRC = REPO / "src"

# The full unions. A NEW value belongs HERE and in a NEW migration, together.
EVENT_TYPES = {
    "STOPS_PLACED", "BREAKEVEN_MOVED", "TRAIL_UPDATED", "STOP_TIGHTENED",
    "TIME_STOP_ARMED", "INVALIDATION_ARMED", "INVALIDATION_FIRED",
    "EARNINGS_BLACKOUT_FLAGGED", "OVERNIGHT_HOLD_DECISION",
    "HALT_FROZEN", "HALT_RESUMED", "SCALE_OUT", "EXIT", "GUARD_ACTION",
    "CORPORATE_ACTION_ADJ", "RECONCILED",
    "POSITION_REVIEW", "STALE_FLAG",          # added 004, lost 007/008, restored 009
    "FORCE_FLAT",                             # added 007
    "PROMOTED",                               # added 008
}
EXIT_LAYERS = {
    "STOP", "CATASTROPHE", "BREAKEVEN", "TRAIL", "TIME", "TARGET",
    "INVALIDATION", "GUARD", "REVIEW", "EARNINGS", "OVERNIGHT",
    "BREAKER", "KILL", "OPERATOR",
    "FORCE_FLAT",                             # added 007
}
STAGES = {
    "TRIAGE", "ANALYST", "GATE", "RISK", "ORDER", "GUARD",
    "PREMARKET", "POSITION_REVIEW", "SYSTEM",
    "CHAT",                                   # added 002
    "THEMATIC",                               # added 004
}


def newest_rebuild(constraint: str) -> tuple[set[str], str]:
    """(value set, filename) from the highest-numbered migration that
    re-ADDs the constraint."""
    best = (-1, None, None)
    for f in sorted(MIGRATIONS.glob("*.sql")):
        m = re.match(r"0*(\d+)", f.name)
        if not m:
            continue
        sql = f.read_text()
        if f"ADD CONSTRAINT {constraint}" in sql and int(m.group(1)) > best[0]:
            block = sql.split(f"ADD CONSTRAINT {constraint}", 1)[1]
            block = block.split(";", 1)[0]
            best = (int(m.group(1)), set(re.findall(r"'([A-Z_]+)'", block)),
                    f.name)
    assert best[1] is not None, f"no migration rebuilds {constraint}"
    return best[1], best[2]


def code_event_types() -> set[str]:
    """Every event_type literal the code can write to position_events."""
    found = set()
    for f in SRC.rglob("*.py"):
        text = f.read_text()
        # positional: position_event(pid, "TYPE", ...)
        found |= set(re.findall(r'position_event\(\s*[^,)]+,\s*"([A-Z_]+)"',
                                text))
        # keyword: event_type="TYPE"
        found |= set(re.findall(r'event_type\s*=\s*"([A-Z_]+)"', text))
        # the engine's basis->event map values
        found |= set(re.findall(r'"(BREAKEVEN_MOVED|TRAIL_UPDATED|'
                                r'STOP_TIGHTENED)"', text))
    return found


def test_position_events_union_never_shrinks():
    values, fname = newest_rebuild("position_events_event_type_check")
    missing = EVENT_TYPES - values
    assert not missing, (f"{fname} rebuilt position_events_event_type_check "
                         f"WITHOUT {sorted(missing)} — a rebuild must start "
                         "from the live constraint, not the base schema "
                         "(see migration 009 header)")


def test_exit_layers_union_never_shrinks():
    values, fname = newest_rebuild("exits_exit_layer_check")
    missing = EXIT_LAYERS - values
    assert not missing, f"{fname} dropped exit layers {sorted(missing)}"


def test_stages_union_never_shrinks():
    values, fname = newest_rebuild("decisions_stage_check")
    missing = STAGES - values
    assert not missing, f"{fname} dropped stages {sorted(missing)}"


def test_every_event_type_the_code_writes_is_allowed():
    values, fname = newest_rebuild("position_events_event_type_check")
    used = code_event_types()
    assert used, "regexes found no event types — patterns need updating"
    unknown = used - values
    assert not unknown, (f"code writes event types {sorted(unknown)} that "
                         f"{fname} does not allow — the a6-nightly bug "
                         "class; add them via a NEW migration")


def test_code_scan_actually_sees_the_known_writers():
    # Guards the scanner itself: these are written by code today.
    used = code_event_types()
    for expected in ("POSITION_REVIEW", "STALE_FLAG", "RECONCILED",
                     "SCALE_OUT", "BREAKEVEN_MOVED"):
        assert expected in used, f"regex scan lost sight of {expected}"
