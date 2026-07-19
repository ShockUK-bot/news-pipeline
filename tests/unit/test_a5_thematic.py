"""A5 unit tests — DB-free: thematic-update contract + grammar-safety,
op resolution (unknown-thesis downgrade, anchor precedence), thesis-id
minting, staleness cutoff, prompt construction, digest renderer."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from a5_thematic.prompt import build_messages
from a5_thematic.render import render_digest, subject_line
from a5_thematic.schema import (ThematicValidationError, ThematicUpdate,
                                thematic_json_schema, validate_thematic)
from a5_thematic.service import resolve_ops
from a5_thematic.store import make_thesis_id, stale_cutoff


def _update(**over):
    base = {"items": [{"item_id": "n:1", "op": "evidence",
                       "thesis_id": "th-2026-001", "polarity": "supports",
                       "note": "capex confirmation"}],
            "new_theses": [],
            "reviews": [],
            "summary": "One evidence attachment tonight."}
    base.update(over)
    return json.dumps(base)


def _new_thesis(anchor="n:2", ticker="VRT"):
    return {"anchor_item_id": anchor, "title": "Grid capex supercycle",
            "driver": "Multi-year utility grid spend accelerates on load "
                      "growth; equipment backlogs stretch into 2028.",
            "direction": "up", "horizon": "LONG", "confidence": 0.55,
            "beneficiaries": [{"ticker": ticker, "relation": "pure play",
                               "rationale": "grid equipment backlog"}],
            "invalidation": ["utility capex guidance cuts"]}


# --- contract --------------------------------------------------------------

def test_update_valid():
    u = validate_thematic(_update())
    assert u.items[0].op == "evidence"
    assert u.items[0].polarity == "supports"


def test_update_rejects_bad_op_polarity_and_garbage():
    with pytest.raises(ThematicValidationError):
        validate_thematic(_update(items=[{"item_id": "n:1", "op": "trade",
                                          "thesis_id": "t"}]))
    with pytest.raises(ThematicValidationError):
        validate_thematic(_update(items=[{"item_id": "n:1", "op": "evidence",
                                          "thesis_id": "t",
                                          "polarity": "bullish"}]))
    with pytest.raises(ThematicValidationError):
        validate_thematic("nope")


def test_new_thesis_ticker_normalized_and_implausible_rejected():
    u = validate_thematic(_update(new_theses=[_new_thesis(ticker=" vrt ")]))
    assert u.new_theses[0].beneficiaries[0].ticker == "VRT"
    with pytest.raises(ThematicValidationError):
        validate_thematic(_update(new_theses=[_new_thesis(ticker="V R T!")]))


def test_schema_grammar_safe():
    s = json.dumps(thematic_json_schema())
    assert "anyOf" not in s


# --- op resolution ---------------------------------------------------------

def _upd(items=(), new=(), reviews=()):
    return ThematicUpdate(items=list(items), new_theses=list(new),
                          reviews=list(reviews), summary="s")


def test_resolve_downgrades_unknown_thesis_and_filters_unclaimed():
    u = _upd(items=[{"item_id": "n:1", "op": "evidence",
                     "thesis_id": "th-2026-099", "polarity": "supports"},
                    {"item_id": "ghost", "op": "ignore"}])
    ops, new, reviews, downgraded = resolve_ops(u, {"th-2026-001"}, ["n:1"])
    assert downgraded == 1
    assert ops["n:1"]["op"] == "ignore"
    assert "ghost" not in ops


def test_resolve_anchor_precedence_and_review_filter():
    u = _upd(items=[{"item_id": "n:2", "op": "evidence",
                     "thesis_id": "th-2026-001", "polarity": "supports"}],
             new=[_new_thesis(anchor="n:2")],
             reviews=[{"thesis_id": "th-2026-001", "op": "keep",
                       "confidence": 0.7},
                      {"thesis_id": "th-2026-777", "op": "invalidate",
                       "confidence": 0.2}])
    ops, new, reviews, _ = resolve_ops(u, {"th-2026-001"}, ["n:2"])
    assert len(new) == 1 and "n:2" not in ops     # anchor wins
    assert [r.thesis_id for r in reviews] == ["th-2026-001"]


# --- store pure helpers ----------------------------------------------------

def test_thesis_id_format():
    assert make_thesis_id(2026, 7) == "th-2026-007"
    assert make_thesis_id(2026, 123) == "th-2026-123"


def test_stale_cutoff_math():
    now = datetime(2026, 7, 19, tzinfo=timezone.utc)
    assert now - stale_cutoff(now, 6) == timedelta(weeks=6)


# --- prompt + renderer -----------------------------------------------------

def test_prompt_deep_marker_and_retry():
    m = build_messages([], [{"item_id": "n:1"}], deep=True,
                       retry_error="bad json")
    assert m[0]["role"] == "system"
    assert "Deep pass" in m[1]["content"]
    assert "previous response was invalid" in m[1]["content"]
    m2 = build_messages([], [], deep=False)
    assert "Deep pass" not in m2[1]["content"]


def test_digest_render_and_subjects():
    stats = {"deep": False, "processed": 3, "new_theses": 1,
             "evidence_attached": 1, "ignored": 1, "expired_bulk": 4,
             "left_queued": 0, "active_after": 2}
    body = render_digest(
        "2026-07-19", "Summary line.",
        [{"thesis_id": "th-2026-001", "direction": "up", "confidence": 0.55,
          "title": "Grid capex supercycle",
          "beneficiaries": [{"ticker": "VRT"}]}],
        [{"thesis_id": "th-2026-001", "polarity": "supports",
          "item_id": "n:1", "headline": "Utility capex raised"}],
        [], [{"thesis_id": "th-2026-000", "title": "Old theme"}],
        stats, "heavy")
    assert "th-2026-001" in body and "VRT" in body
    assert "EXPIRED (staleness rule)" in body
    assert "2 active theses" in body
    assert "new thesis" in subject_line("2026-07-19", stats)
    quiet = {"new_theses": 0, "evidence_attached": 0}
    assert "no store changes" in subject_line("2026-07-19", quiet)
