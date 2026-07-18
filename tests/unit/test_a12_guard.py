"""A12 unit tests — DB-free: verdict contract, cross-field discipline, prompt
content, position pack / context math, wake probe logic."""
import asyncio
import json
from datetime import timedelta

import pytest

from common.clock import utcnow
from common.marketdata import FakeData, Quote
from a12_guard.context import build_guard_context, position_pack
from a12_guard.prompt import SYSTEM_PROMPT, build_messages
from a12_guard.schema import (ACTION_MAP, GuardValidationError,
                              guard_json_schema, validate_guard)
from a12_guard.wake import ensure_model_up


def _verdict(**over):
    base = {"thesis_intact": True, "recommended_action": "hold",
            "urgency": "low", "confidence": 0.7, "watch_hits": [],
            "reason": "no thesis impact"}
    base.update(over)
    return json.dumps(base)


# --- schema ----------------------------------------------------------------

def test_valid_verdict_parses():
    v = validate_guard(_verdict())
    assert v.thesis_intact is True
    assert v.recommended_action == "hold"


def test_exit_verdict_with_watch_hits():
    v = validate_guard(_verdict(thesis_intact=False, recommended_action="exit",
                                urgency="high",
                                watch_hits=["counterparty_denial"]))
    assert v.watch_hits == ["counterparty_denial"]
    assert ACTION_MAP[v.recommended_action] == "EXIT"


def test_risk_increasing_action_is_not_expressible():
    """Baseline rules 12/16: the schema's only actions are hold, tighten_stop,
    exit — widen/add/extend are schema violations, not policy checks."""
    for bad in ("widen_stop", "add", "extend_window", "buy_more"):
        with pytest.raises(GuardValidationError):
            validate_guard(_verdict(recommended_action=bad))


def test_missing_field_and_bad_confidence_rejected():
    raw = json.loads(_verdict())
    del raw["reason"]
    with pytest.raises(GuardValidationError):
        validate_guard(json.dumps(raw))
    with pytest.raises(GuardValidationError):
        validate_guard(_verdict(confidence=1.4))


def test_not_json_rejected():
    with pytest.raises(GuardValidationError):
        validate_guard("the thesis is fine, hold")


def test_schema_is_grammar_safe():
    """llama.cpp grammar limits (a13 deploy guide §3): flat object, no
    anyOf/null sub-objects, string bounds ≤600."""
    schema = guard_json_schema()
    assert "$defs" not in json.dumps(schema.get("properties", {}))
    assert schema["properties"]["reason"]["maxLength"] <= 600
    assert "anyOf" not in json.dumps(schema)


# --- prompt ----------------------------------------------------------------

def test_prompt_carries_watchlist_and_position():
    pos = {"ticker": "ACME", "qty_open": 50, "avg_entry": 100.0,
           "current_stop": 96.0, "watch_list": ["counterparty_denial"]}
    item = {"headline": "Acme deal denied", "is_correction": False}
    msgs = build_messages(item, pos, {"direction": "up", "reason": "deal"},
                          {"price_action": {"last": 98.0}})
    user = msgs[1]["content"]
    assert "counterparty_denial" in user
    assert "Acme deal denied" in user
    assert '"avg_entry": 100.0' in user
    assert "LONG-ONLY" in SYSTEM_PROMPT
    assert "cannot widen stops" in SYSTEM_PROMPT


def test_prompt_retry_appends_error():
    msgs = build_messages({"headline": "x"}, {}, {}, {}, retry_error="bad enum")
    assert "bad enum" in msgs[1]["content"]


# --- position pack / context math ------------------------------------------

def test_position_pack_reads_live_policy_state():
    pos = {"ticker": "ACME", "horizon": "SHORT", "profile": "short_term_v1",
           "qty_open": 50, "avg_entry": 100.0, "initial_stop": 96.0,
           "opened_ts": utcnow() - timedelta(days=2),
           "exit_policy": {"current_stop": 98.5, "scale_out_done": True,
                           "news_invalidations": ["fda_timeline_slip"]}}
    pack = position_pack(pos)
    assert pack["current_stop"] == 98.5          # live state, not initial
    assert pack["scale_out_done"] is True
    assert pack["watch_list"] == ["fda_timeline_slip"]
    assert pack["opened_days_ago"] == 2.0


def test_position_pack_falls_back_to_initial_stop():
    pos = {"ticker": "ACME", "qty_open": 50, "avg_entry": 100.0,
           "initial_stop": 96.0, "opened_ts": None, "exit_policy": {}}
    assert position_pack(pos)["current_stop"] == 96.0


@pytest.mark.asyncio
async def test_context_computes_unrealized_r_and_move():
    md = FakeData()
    now = utcnow()
    md.set_quote("ACME", Quote(price=104.0, bid=103.98, ask=104.02, ts=now))
    received = now - timedelta(minutes=20)
    md.set_minute("ACME", FakeData.ramp_minute(
        received - timedelta(minutes=30), 30, 100.0, 100.0, 10_000))
    item = {"received_ts": received.isoformat(),
            "published_ts": received.isoformat()}
    pos = {"ticker": "ACME", "avg_entry": 100.0, "r_unit": 4.0}
    ctx = await build_guard_context(md, item, pos)
    pa = ctx["price_action"]
    assert pa["unrealized_r"] == 1.0             # (104-100)/4
    assert pa["pct_move_since_news"] == pytest.approx(0.04, abs=1e-4)
    assert pa["minutes_since_news"] == 20


@pytest.mark.asyncio
async def test_context_degrades_when_marketdata_fails():
    class DeadMD:
        async def snapshot(self, s):
            raise RuntimeError("down")
        async def minute_bars(self, s, a, b):
            raise RuntimeError("down")
    item = {"received_ts": utcnow().isoformat(),
            "published_ts": utcnow().isoformat()}
    ctx = await build_guard_context(DeadMD(), item,
                                    {"ticker": "ACME", "avg_entry": 100.0,
                                     "r_unit": 4.0})
    assert ctx["price_action"]["last"] is None
    assert ctx["price_action"]["unrealized_r"] is None


# --- wake ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wake_short_circuits_when_up(monkeypatch):
    async def up(endpoint, timeout=3.0):
        return True
    monkeypatch.setattr("a12_guard.wake.probe_health", up)
    ran = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        lambda *a, **k: ran.append(a))
    assert await ensure_model_up("http://x:8081", {"enabled": True}) is True
    assert ran == []                             # probe-first: never woke


@pytest.mark.asyncio
async def test_wake_disabled_down_is_alert_only():
    # nothing listens on port 9 — probe fails fast, wake disabled -> False
    ok = await ensure_model_up("http://127.0.0.1:9",
                               {"enabled": False, "probe_timeout_secs": 0.3})
    assert ok is False
