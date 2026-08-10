"""v0.12.23 — A5 bootstrap mode, wide pass, and the config pins that stop
this class of bug coming back.

Background: from 2026-07-19 to 2026-08-10 A5 processed 113 thesis-lane
items and produced 113 IGNOREs and zero theses. Root cause was the prompt,
not the model: with an empty store, "attach as evidence to an existing
thesis" is impossible, and the remaining instruction said new theses are
RARE. These tests pin the escape hatch.
"""
from pathlib import Path

import pytest
import yaml

from a5_thematic.prompt import build_messages, system_prompt
from a5_thematic.schema import (ItemOp, NewThesis, ThematicUpdate,
                                ThesisReview, Beneficiary)
from a5_thematic.service import resolve_ops

CONFIG = Path(__file__).resolve().parents[2] / "config" / "a5.yaml"


# --- prompt mode selection -------------------------------------------------

def test_bootstrap_prompt_demands_theses():
    p = system_prompt(0, bootstrap=True, target=5)
    assert "BOOTSTRAP" in p
    assert "FAILURE" in p                      # zero theses is not caution
    assert "5 new standing theses" in p
    # the deadlock wording must NOT survive into bootstrap mode
    assert "New theses are RARE" not in p


def test_steady_prompt_keeps_the_conservative_rule():
    p = system_prompt(7, bootstrap=False, target=5)
    assert "New theses are RARE" in p
    assert "BOOTSTRAP" not in p
    assert "7 active theses" in p


def test_singular_plural_does_not_read_broken():
    assert "1 active thesis" in system_prompt(1, bootstrap=True, target=4)
    assert "0 active theses" in system_prompt(0, bootstrap=True, target=4)


def test_ignore_note_is_mandatory_in_both_modes():
    for p in (system_prompt(0, True, 5), system_prompt(9, False, 5)):
        assert "ignore" in p and "empty note is not acceptable" in p


# --- message assembly ------------------------------------------------------

def test_bootstrap_reminder_and_status_ride_in_the_user_turn():
    m = build_messages([], [{"item_id": "n:1"}], deep=True, bootstrap=True,
                       bootstrap_target=5)
    assert '"store_status": "bootstrap"' in m[1]["content"]
    assert "Seed 5 new ones" in m[1]["content"]
    assert "failed run" in m[1]["content"]


def test_no_bootstrap_reminder_when_populated():
    m = build_messages([{"thesis_id": "th-2026-001"}], [], deep=False)
    assert "failed run" not in m[1]["content"]
    assert '"store_status": "populated"' in m[1]["content"]


def test_week_context_included_only_when_supplied():
    ctx = {"open_positions": [{"ticker": "AAPL"}]}
    with_ctx = build_messages([], [], deep=True, context=ctx)
    assert "week_in_review" in with_ctx[1]["content"]
    assert "AAPL" in with_ctx[1]["content"]
    assert "week_in_review" not in build_messages([], [], deep=True)[1]["content"]


def test_macro_context_included_only_when_supplied():
    """v0.12.24: the macro block rides in its own top-level key, absent
    entirely when macro data is unavailable (pre-v0.12.24 behavior)."""
    macro = {"as_of": "2026-08-10",
             "groups": {"rates_curve": [{"label": "10y Treasury yield",
                                         "latest": 4.25}]}}
    m = build_messages([], [], deep=False, macro=macro)
    assert "macro_context" in m[1]["content"]
    assert "10y Treasury yield" in m[1]["content"]
    assert "macro_context" not in build_messages([], [], deep=False)[1]["content"]


def test_prompt_explains_macro_context_in_both_modes():
    for p in (system_prompt(0, True, 5), system_prompt(9, False, 5)):
        assert "macro_context" in p
        # macro is context, never a thesis subject by itself
        assert "theses need equity beneficiaries" in p


def test_deep_marker_and_retry_still_work():
    m = build_messages([], [{"item_id": "n:1"}], deep=True,
                       retry_error="bad json", bootstrap=True)
    assert "Deep pass" in m[1]["content"]
    assert "previous response was invalid" in m[1]["content"]


# --- resolve_ops with read-only wide items ---------------------------------

def _update(items=(), new=(), reviews=()):
    return ThematicUpdate(items=list(items), new_theses=list(new),
                          reviews=list(reviews), summary="s")


def _thesis(anchor):
    return NewThesis(anchor_item_id=anchor, title="T", driver="D",
                     direction="up", confidence=0.5,
                     beneficiaries=[Beneficiary(ticker="AAPL", relation="r",
                                                rationale="x")],
                     invalidation=["deal collapses"])


def test_wide_item_may_anchor_a_new_thesis():
    u = _update(new=[_thesis("wide:1")])
    _, new, _, _ = resolve_ops(u, set(), claimed_item_ids=["lane:1"],
                               anchorable_item_ids=["lane:1", "wide:1"])
    assert [t.anchor_item_id for t in new] == ["wide:1"]


def test_wide_anchor_dropped_when_not_addressable():
    """Back-compat: the 3-arg call behaves exactly as it did pre-v0.12.23."""
    u = _update(new=[_thesis("wide:1")])
    _, new, _, _ = resolve_ops(u, set(), ["lane:1"])
    assert new == []


def test_wide_item_may_receive_evidence():
    u = _update(items=[ItemOp(item_id="wide:1", op="evidence",
                              thesis_id="th-2026-001", polarity="supports",
                              note="n")])
    ops, _, _, downgraded = resolve_ops(
        u, {"th-2026-001"}, ["lane:1"], ["lane:1", "wide:1"])
    assert ops["wide:1"]["op"] == "evidence"
    assert downgraded == 0


def test_unknown_thesis_still_downgrades_on_a_wide_item():
    u = _update(items=[ItemOp(item_id="wide:1", op="evidence",
                              thesis_id="th-9999-999")])
    ops, _, _, downgraded = resolve_ops(
        u, {"th-2026-001"}, ["lane:1"], ["lane:1", "wide:1"])
    assert ops["wide:1"]["op"] == "ignore" and downgraded == 1


# --- config pins (the regressions that caused this outage) -----------------

def test_config_token_budget_survived_the_truncation_fix():
    """All five historical REJECTs were truncation at ~8.2k chars against a
    3000-token cap, not bad grammar. Never go back."""
    cfg = yaml.safe_load(CONFIG.read_text())
    assert cfg["narrative"]["max_tokens"] >= 8000


def test_wide_cap_cannot_exceed_the_schema_bound():
    """ItemOp list max_length is 80; a longer items list is a hard schema
    violation and would fail the whole pass."""
    cfg = yaml.safe_load(CONFIG.read_text())
    bound = ThematicUpdate.model_fields["items"].metadata[0].max_length
    assert bound == 80
    assert cfg["lane"]["wide_max_items"] <= bound


def test_bootstrap_floor_is_configured_and_sane():
    cfg = yaml.safe_load(CONFIG.read_text())
    assert 1 <= cfg["store"]["bootstrap_min_theses"] <= 10
    assert 1 <= cfg["store"]["bootstrap_target"] <= 6   # NewThesis list max 6
