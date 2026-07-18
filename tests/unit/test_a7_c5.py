"""Phase 6 unit tests — DB-free: narrative contract, renderer determinism
(empty day and busy day), subject lines, slot-resolution order + heavy
ownership rule, mailer transport selection guards."""
import asyncio
import json
import os

import pytest

from a7_report.narrative import (NarrativeValidationError,
                                 narrative_json_schema, validate_narrative)
from a7_report.render import render, subject_line
from a7_report.service import SlotManager
from c5_mailer.service import SmtpTransport


def _narr(**over):
    base = {"summary": "No trades today. All vetoes were LONG_ONLY.",
            "notables": ["marketdata heartbeat DEGRADED overnight"],
            "data_quality": "ok"}
    base.update(over)
    return json.dumps(base)


EMPTY_FACTS = {
    "session_date": "2026-07-20",
    "window": {"start": "2026-07-20T04:00:00+00:00",
               "end": "2026-07-20T21:35:00+00:00"},
    "activity": {"items_ingested": 3400,
                 "stage_counts": {"TRIAGE": {"ESCALATE": 40, "DISCARD": 300,
                                             "SUPPRESS": 60},
                                  "ANALYST": {"THESIS": 12},
                                  "GATE": {}},
                 "vetoes": [{"stage": "GATE", "reason": "LONG_ONLY",
                             "count": 8}],
                 "quarantined_today": 0},
    "trades": {"opened": [], "exits": [], "orders_by_role": {},
               "realized_pnl_today": 0.0, "pnl_by_exit_layer": []},
    "open_positions": [],
    "guard": {"verdicts": [], "alert_only_count": 0},
    "controls": {"kill_switch": "0", "drawdown_breaker": "0",
                 "block_entries": "0", "trading_capital": "50000",
                 "max_trades_per_day": "5"},
    "health_not_ok": [],
    "ingestion_gaps": [],
}


def busy_facts():
    f = json.loads(json.dumps(EMPTY_FACTS))
    f["trades"]["opened"] = [{"position_id": 1, "ticker": "ACME", "qty": 50,
                              "horizon": "SHORT", "avg_entry": 100.0,
                              "initial_stop": 96.0,
                              "opened_ts": "2026-07-20T14:45:00+00:00",
                              "headline": "Acme wins defense contract"}]
    f["trades"]["exits"] = [{"position_id": 1, "ticker": "ACME", "qty": 25,
                             "price": 104.0, "layer": "TARGET",
                             "realized_pnl": 100.0, "r_multiple": 1.0,
                             "is_partial": True,
                             "ts": "2026-07-20T19:10:00+00:00"}]
    f["trades"]["realized_pnl_today"] = 100.0
    f["trades"]["pnl_by_exit_layer"] = [{"layer": "TARGET", "count": 1,
                                         "realized_pnl": 100.0}]
    f["open_positions"] = [{"position_id": 1, "ticker": "ACME", "qty_open": 25,
                            "horizon": "SHORT", "avg_entry": 100.0,
                            "last_price": 103.5, "unrealized_pnl": 87.5,
                            "unrealized_r": 0.88, "current_stop": 100.0,
                            "stop_basis": "BREAKEVEN",
                            "realized_pnl_partial": 100.0,
                            "opened_ts": "2026-07-20T14:45:00+00:00"}]
    f["guard"] = {"verdicts": [{"position_id": 1, "ticker": "ACME",
                                "thesis_intact": True,
                                "recommended_action": "HOLD",
                                "urgency": "low",
                                "ts": "2026-07-20T18:00:00+00:00"}],
                  "alert_only_count": 0}
    return f


# --- narrative contract ----------------------------------------------------

def test_narrative_valid():
    n = validate_narrative(_narr())
    assert n.data_quality == "ok"
    assert len(n.notables) == 1


def test_narrative_rejects_bad_shapes():
    with pytest.raises(NarrativeValidationError):
        validate_narrative("not json")
    with pytest.raises(NarrativeValidationError):
        validate_narrative(_narr(data_quality="excellent"))
    with pytest.raises(NarrativeValidationError):
        validate_narrative(json.dumps({"summary": ""}))


def test_narrative_schema_grammar_safe():
    s = json.dumps(narrative_json_schema())
    assert "anyOf" not in s
    assert narrative_json_schema()["properties"]["summary"]["maxLength"] <= 600


# --- renderer --------------------------------------------------------------

def test_render_empty_day_without_narrative():
    body = render(EMPTY_FACTS, None)
    assert "No positions opened or exited today." in body
    assert "narrative unavailable" in body
    assert "None (flat)." in body
    assert "LONG_ONLY×8" in body
    assert "all clear" in body
    assert subject_line(EMPTY_FACTS) == "EOD Report 2026-07-20 — no trades"


def test_render_busy_day_with_narrative():
    n = validate_narrative(_narr(summary="One partial win on ACME."))
    body = render(busy_facts(), n)
    assert "One partial win on ACME." in body
    assert "OPENED ACME 50 @ $100.00" in body
    assert "trigger: Acme wins defense contract" in body
    assert "via TARGET (partial): $100.00 (+1.00R" in body
    assert "thesis intact -> HOLD" in body
    assert "stop $100.00 [BREAKEVEN]" in body
    assert "no trades" not in subject_line(busy_facts())
    assert "realized $100.00" in subject_line(busy_facts())


def test_render_flags_and_negative_pnl():
    f = json.loads(json.dumps(EMPTY_FACTS))
    f["controls"]["kill_switch"] = "1"
    f["trades"]["realized_pnl_today"] = -125.5
    f["health_not_ok"] = [{"component": "marketdata", "status": "DEGRADED",
                           "detail": "stale"}]
    body = render(f, None)
    assert "KILL SWITCH" in body
    assert "-$125.50" in body
    assert "HEALTH DEGRADED: marketdata" in body


# --- slot resolution -------------------------------------------------------

def _slot_cfg():
    return {"narrative": {"max_tokens": 900},
            "heavy": {"endpoint": "http://h:8084", "model_id": "heavy-m",
                      "autostart": True, "stop_after_use": True,
                      "start_command": "sudo systemctl start llama-heavy.service",
                      "stop_command": "sudo systemctl stop llama-heavy.service",
                      "ready_timeout_secs": 1, "poll_secs": 0.05,
                      "probe_timeout_secs": 0.1},
            "analyst_fallback": {"enabled": True, "endpoint": "http://a:8081",
                                 "model_id": "analyst-m",
                                 "wake": {"enabled": False}}}


@pytest.mark.asyncio
async def test_slots_prefer_running_heavy():
    async def probe(endpoint, timeout=1.0):
        return "8084" in endpoint
    ran = []
    async def runner(cmd):
        ran.append(cmd)
        return True
    sm = SlotManager(_slot_cfg(), probe=probe, runner=runner)
    backend, name = await sm.acquire()
    assert name == "heavy" and backend.model_id == "heavy-m"
    assert ran == []                              # probe-first: no start
    await sm.release()
    assert ran == []                              # didn't start -> mustn't stop


@pytest.mark.asyncio
async def test_slots_start_heavy_then_stop_it():
    state = {"up": False}
    async def probe(endpoint, timeout=1.0):
        return "8084" in endpoint and state["up"]
    async def runner(cmd):
        if cmd.endswith("start llama-heavy.service"):
            state["up"] = True
        if cmd.endswith("stop llama-heavy.service"):
            state["up"] = False
        return True
    sm = SlotManager(_slot_cfg(), probe=probe, runner=runner)
    backend, name = await sm.acquire()
    assert name == "heavy" and sm.started_heavy
    await sm.release()
    assert state["up"] is False                   # ownership rule: we stop it


@pytest.mark.asyncio
async def test_slots_fall_back_to_analyst(monkeypatch):
    async def probe(endpoint, timeout=1.0):
        return False                              # heavy never comes up
    async def runner(cmd):
        return True
    async def fake_ensure(endpoint, wake):
        return True
    monkeypatch.setattr("a7_report.service.ensure_model_up", fake_ensure)
    sm = SlotManager(_slot_cfg(), probe=probe, runner=runner)
    backend, name = await sm.acquire()
    assert name == "analyst" and backend.model_id == "analyst-m"
    await sm.release()                            # heavy never started: no stop


@pytest.mark.asyncio
async def test_slots_none_available(monkeypatch):
    async def probe(endpoint, timeout=1.0):
        return False
    async def runner(cmd):
        return False                              # start command fails too
    async def fake_ensure(endpoint, wake):
        return False
    monkeypatch.setattr("a7_report.service.ensure_model_up", fake_ensure)
    backend, name = await SlotManager(_slot_cfg(), probe=probe,
                                      runner=runner).acquire()
    assert (backend, name) == (None, "none")


# --- mailer transport ------------------------------------------------------

def test_transport_unconfigured_detected():
    t = SmtpTransport(env={})
    assert not t.configured()


def test_transport_reads_env_only():
    t = SmtpTransport(env={"MAILER_SMTP_HOST": "smtp.gmail.com",
                           "MAILER_SMTP_PORT": "465",
                           "MAILER_SMTP_USER": "x@gmail.com",
                           "MAILER_SMTP_PASS": "app-pass",
                           "MAILER_FROM": "Pipeline <x@gmail.com>",
                           "MAILER_TO": "a@x.com, b@y.com"})
    assert t.configured()
    assert t.mail_to == ["a@x.com", "b@y.com"]
    assert t.port == 465
