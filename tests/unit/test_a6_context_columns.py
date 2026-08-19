"""A6 context-pack guards — DB-free.

Origin: v0.13.0 added `p.side` to A6's `load_open_positions` SELECT but not
to the hand-written key tuple it was zipped with. `zip()` truncates
silently, so every key from index 14 on shifted by one: `thesis_payload`
received the side string, `thesis_reason` received the payload dict, and
`d.confidence` was dropped entirely. `build_pack` then did
`"SHORT".get("thesis")` -> AttributeError, killing BOTH a6-eod and
a6-nightly on every run with an open position (found by the C7 watchdog,
2026-08-19).

Two guards, in the spirit of v0.12.8's `test_schema_vocab`: fix the class,
not just the instance.

  1. STRUCTURAL — no reader in src/ may hand-write a key tuple that
     disagrees with its own SELECT list. The house convention is
     `cols = [d.name for d in cur.description]`, which cannot drift.
  2. BEHAVIOURAL — `build_pack` survives a realistic row and carries a
     sign-correct `r_progress` for a SHORT.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from a6_position_review import context as ctx

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


# --- 1. structural: SELECT list vs. hand-written key tuple -----------------

def _select_list(sql: str) -> list[str] | None:
    """Top-level select-list expressions, or None if not verifiable."""
    m = re.search(r"\bSELECT\b(\s+DISTINCT\b)?", sql, re.I)
    if not m:
        return None
    i, depth, start, out = m.end(), 0, m.end(), []
    while i < len(sql):
        c = sql[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(sql[start:i].strip())
            start = i + 1
        elif depth == 0 and re.match(r"\s FROM\b", sql[i:i + 6], re.I):
            break
        i += 1
    else:
        return None
    out.append(sql[start:i].strip())
    if any(c == "*" or c.endswith(".*") for c in out):
        return None
    return out


def _hand_written_col_sites():
    """(file, function, select_list, key_tuple) for every function that both
    builds a SQL SELECT and assigns a literal `cols` tuple/list."""
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            cols = None
            for node in ast.walk(fn):
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id == "cols"
                        and isinstance(node.value, (ast.Tuple, ast.List))
                        and node.value.elts
                        and all(isinstance(e, ast.Constant)
                                for e in node.value.elts)):
                    cols = [e.value for e in node.value.elts]
            if cols is None:
                continue
            sqls = [n.value for n in ast.walk(fn)
                    if isinstance(n, ast.Constant)
                    and isinstance(n.value, str)
                    and re.search(r"\bSELECT\b", n.value, re.I)]
            if not sqls:
                continue
            sel = _select_list(max(sqls, key=len))
            if sel is not None:
                yield path.relative_to(SRC.parent), fn.name, sel, cols


def test_no_column_list_drift():
    """Every hand-written key tuple has exactly one name per selected column."""
    drifted = [(f"{p}:{fn}", len(sel), len(cols))
               for p, fn, sel, cols in _hand_written_col_sites()
               if len(sel) != len(cols)]
    assert not drifted, (
        "SELECT list and key tuple disagree — zip() will silently misalign "
        "every key after the insertion point: "
        + "; ".join(f"{w} selects {a}, names {b}" for w, a, b in drifted))


def test_a6_loader_produces_every_key_its_callers_read():
    """A6's loader must expose side + the aliased thesis columns. Read from
    the SQL text so this holds however the keys are derived."""
    src = pathlib.Path(ctx.__file__).read_text()
    body = src[src.index("async def load_open_positions"):]
    body = body[:body.index("\nasync def ", 1)]
    for required in ("p.side", "AS thesis_payload", "AS thesis_reason",
                     "AS thesis_confidence"):
        assert required in body, f"{required!r} missing from A6's SELECT"


# --- 2. behavioural: build_pack over a realistic row -----------------------

def _row(side: str) -> dict:
    """A row shaped exactly like the loader's output for an open position."""
    opened = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)
    return {"position_id": 7, "ticker": "SNDK", "horizon": "SHORT",
            "profile": "news_short", "opened_ts": opened,
            "qty_initial": 100, "qty_open": 100, "avg_entry": 50.0,
            "initial_stop": 53.0, "r_unit": 3.0, "exit_policy": None,
            "last_price": 44.0, "realized_pnl": 0.0,
            "thesis_decision_id": 42, "side": side,
            "thesis_payload": {"thesis": {"magnitude_est": 6.0}},
            "thesis_reason": "guidance cut", "thesis_confidence": 0.7}


def _patch(monkeypatch):
    async def _news(*a, **k):
        return {"escalated_since_entry": 1,
                "last_escalation_ts": "2026-08-18T13:00:00+00:00",
                "days_since_news": 1.0}

    async def _empty_list(*a, **k):
        return []

    monkeypatch.setattr(ctx, "ticker_news_recency", _news)
    monkeypatch.setattr(ctx, "guard_activity", _empty_list)
    monkeypatch.setattr(ctx, "thesis_store_matches", _empty_list)
    monkeypatch.setattr(ctx, "sessions_held", lambda *a, **k: 2)


def test_build_pack_survives_a_real_row_and_is_side_aware(monkeypatch):
    _patch(monkeypatch)
    now = datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc)

    short = asyncio.run(ctx.build_pack(_row("SHORT"), now, 4.0))
    assert short["side"] == "SHORT"
    assert short["thesis"] == {"magnitude_est": 6.0}      # not the side string
    assert short["thesis_reason"] == "guidance cut"       # not the payload
    # 50 -> 44 on a 3.0 R-unit is +2.0R for a SHORT, never -2.0R
    assert short["r_progress"] == 2.0

    long_ = asyncio.run(ctx.build_pack(_row("LONG"), now, 4.0))
    assert long_["side"] == "LONG" and long_["r_progress"] == -2.0


def test_build_pack_defaults_missing_side_to_long(monkeypatch):
    """Positions opened before v0.13.0 may carry a NULL side."""
    _patch(monkeypatch)
    row = _row("LONG")
    row["side"] = None
    pack = asyncio.run(ctx.build_pack(
        row, datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc), 4.0))
    assert pack["side"] == "LONG"
