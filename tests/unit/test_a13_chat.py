"""A13 unit tests — DB-free by design (same discipline as the other unit
suites: pure functions and schema contracts here, Postgres flows in
integration). Run: pytest tests/unit/test_a13_chat.py"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from a13_chat.filing import anchor_is_fresh
from a13_chat.prompt import build_answer_messages, build_planner_messages
from a13_chat.retrieval import truncate_fact_sheet
from a13_chat.schema import (ChatValidationError, FilingProposal,
                             validate_answer, validate_plan)

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Planner contract
# ---------------------------------------------------------------------------

def test_planner_valid():
    plan = validate_plan(json.dumps({
        "queries": [{"query": "vetoes", "ticker": "nvda", "days": 7},
                    {"query": "decision_trace", "ticker": "NVDA", "days": 7}],
        "reason": "veto question about NVDA",
    }))
    assert plan.queries[0].ticker == "NVDA"          # normalized upper
    assert plan.queries[0].query == "vetoes"


def test_planner_rejects_unknown_pack():
    with pytest.raises(ChatValidationError):
        validate_plan(json.dumps({
            "queries": [{"query": "drop_table"}], "reason": "x"}))


def test_planner_rejects_free_sql_shaped_ticker():
    with pytest.raises(ChatValidationError):
        validate_plan(json.dumps({
            "queries": [{"query": "vetoes", "ticker": "'; DROP--"}],
            "reason": "x"}))


def test_planner_caps_at_five_queries():
    with pytest.raises(ChatValidationError):
        validate_plan(json.dumps({
            "queries": [{"query": "open_positions"}] * 6, "reason": "x"}))


# ---------------------------------------------------------------------------
# Answer contract
# ---------------------------------------------------------------------------

NO_REC = {"stance": "no_view", "rationale": ""}
NO_PROP = {"ticker": "", "anchor_item_id": "", "rationale": ""}


def test_answer_minimal_with_sentinels():
    ans = validate_answer(json.dumps({
        "answer": "No open positions.",
        "recommendation": NO_REC, "filing_proposal": NO_PROP}))
    assert ans.effective_recommendation() is None
    assert ans.effective_proposal() is None


def test_answer_with_recommendation_and_proposal():
    ans = validate_answer(json.dumps({
        "answer": "Fresh tier-1 filing; not held; not previously vetoed.",
        "recommendation": {"stance": "consider_long", "rationale": "8-K, uncrowded"},
        "filing_proposal": {"ticker": "acme", "anchor_item_id": "edgar:123",
                            "rationale": "operator asked; fresh 8-K"},
        "caveats": ["ATR unavailable"],
    }))
    assert ans.effective_proposal().ticker == "ACME"
    assert ans.effective_recommendation().stance == "consider_long"


def test_answer_rejects_bad_stance():
    with pytest.raises(ChatValidationError):
        validate_answer(json.dumps({
            "answer": "x", "filing_proposal": NO_PROP,
            "recommendation": {"stance": "yolo_long", "rationale": "no"}}))


def test_answer_schema_grammar_safe():
    """v0.5.5 regression: the Spark's llama-server rejects grammars from
    schemas with nullable ('anyOf' + null) constructs — keep them out."""
    from a13_chat.schema import answer_json_schema
    s = json.dumps(answer_json_schema())
    assert "anyOf" not in s and '"null"' not in s


# ---------------------------------------------------------------------------
# Filing freshness gate (pure)
# ---------------------------------------------------------------------------

def test_anchor_fresh_inside_window():
    assert anchor_is_fresh(NOW - timedelta(hours=71), NOW, 72)


def test_anchor_stale_outside_window():
    assert not anchor_is_fresh(NOW - timedelta(hours=73), NOW, 72)


def test_proposal_ticker_normalized():
    p = FilingProposal(ticker="brk.b", anchor_item_id="alpaca:1", rationale="r")
    assert p.ticker == "BRK.B"


# ---------------------------------------------------------------------------
# Fact-sheet truncation
# ---------------------------------------------------------------------------

def test_truncate_halves_longest_list_and_records_it():
    fs = {"closed_trades": [{"row": i, "pad": "x" * 50} for i in range(100)],
          "control_state": {"control": []}}
    out = truncate_fact_sheet(fs, max_chars=2000)
    assert len(out["closed_trades"]) < 100
    assert "closed_trades" in out["_truncated"]
    assert len(json.dumps(out, default=str)) <= 2000 + 200  # converges near budget


def test_truncate_noop_under_budget():
    fs = {"open_positions": [{"a": 1}]}
    assert truncate_fact_sheet(fs, 10_000) == fs


# ---------------------------------------------------------------------------
# Prompts carry the question + retry discipline
# ---------------------------------------------------------------------------

def test_planner_messages_shape():
    msgs = build_planner_messages("why was NVDA vetoed?")
    assert msgs[0]["role"] == "system" and "vetoes" in msgs[0]["content"]
    assert "NVDA" in msgs[-1]["content"]


def test_answer_retry_appends_error():
    msgs = build_answer_messages("q", {"open_positions": []}, [],
                                 retry_error="schema violations: answer: missing")
    assert "previous response was invalid" in msgs[-1]["content"]


# ---------------------------------------------------------------------------
# Service fallback plan is always valid
# ---------------------------------------------------------------------------

def test_fallback_plan_valid():
    from a13_chat.service import fallback_plan
    plan = fallback_plan()
    assert 1 <= len(plan.queries) <= 5
    assert {q.query for q in plan.queries} <= {
        "open_positions", "closed_trades", "vetoes", "control_state"}
