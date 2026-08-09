"""v0.12.21 unit tests — stale A6 recommendations in the morning briefing.

Observed 2026-08-09: every briefing since Aug 3 re-served the A6 'exit
JNJ' recommendation. Position 5 closed via TIME stop the morning of
Aug 3, the book went flat, A6 stopped writing REVIEW rows (by design),
and A8's latest-row lookup kept surfacing the last review — whose recos
the narrator then presented as current advice.

Fix under test: a6_section filters recos to the CURRENT open book's
position_ids and adjusts the recommendations count; the dropped count is
recorded honestly as stale_recos_dropped.
"""
import pytest

import a8_briefing.facts as facts_mod
from a8_briefing.facts import a6_section


REVIEW = {
    "run_date": "2026-08-02", "reviewed": 1, "rejected": 0,
    "stale_flagged": 1, "recommendations": 1, "holds": 0,
    "recos": [{"position_id": 5, "ticker": "JNJ", "action": "exit",
               "rationale": "thesis stale; time-stop window exhausted"}],
}


@pytest.fixture
def payloads(monkeypatch):
    store = {"REVIEW": None, "EOD_SHEET": None}

    async def _fake_latest(stage, agent, action):
        return store.get(action)

    monkeypatch.setattr(facts_mod, "_latest_payload", _fake_latest)
    return store


async def test_flat_book_drops_the_stale_reco(payloads):
    # The exact 2026-08-09 shape: book flat, last review still recommends
    # exiting position 5.
    payloads["REVIEW"] = dict(REVIEW)
    out = await a6_section(open_position_ids=[])
    assert out["review"]["recos"] == []
    assert out["review"]["recommendations"] == 0
    assert out["review"]["stale_recos_dropped"] == 1
    assert out["review"]["run_date"] == "2026-08-02"     # history stays


async def test_open_position_keeps_its_reco(payloads):
    payloads["REVIEW"] = dict(REVIEW)
    out = await a6_section(open_position_ids=[5])
    assert out["review"]["recos"] == REVIEW["recos"]
    assert out["review"]["recommendations"] == 1
    assert "stale_recos_dropped" not in out["review"]


async def test_mixed_book_filters_only_closed(payloads):
    payloads["REVIEW"] = {**REVIEW, "recommendations": 2, "recos": [
        REVIEW["recos"][0],
        {"position_id": 7, "ticker": "WIX", "action": "trim",
         "rationale": "r"}]}
    out = await a6_section(open_position_ids=[7])
    assert [r["position_id"] for r in out["review"]["recos"]] == [7]
    assert out["review"]["recommendations"] == 1
    assert out["review"]["stale_recos_dropped"] == 1


async def test_no_review_on_record_stays_none(payloads):
    out = await a6_section(open_position_ids=[])
    assert out["review"] is None
