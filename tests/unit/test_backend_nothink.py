"""v0.12.4 — regression cover for the heavy-slot thinking switch.

Probe evidence (2026-07-24): with thinking enabled the 122B narrates prose
inside its <think> block, response_format never bites, generation hits the
max_tokens cap in reasoning and content comes back empty — every heavy
consumer fell to deterministic fallback (A4 07:00, A6 nightly, A7 15:35 on
2026-07-23). `chat_template_kwargs {"enable_thinking": false}` returned
schema-valid JSON in content at 52 tokens. These tests pin the request
shape so the flag can't silently vanish.
"""
import asyncio
import json

import pytest

from a1_triage.backends import LlamaCppBackend
from a7_report.service import SlotManager


class _Capture:
    def __init__(self):
        self.sent = None

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'},
                                 "finish_reason": "stop"}]}

    def client(self, capture):
        outer = self

        class _Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None):
                capture.sent = json
                return outer._Resp()
        return _Client()


@pytest.fixture
def capture(monkeypatch):
    cap = _Capture()
    import a1_triage.backends as b
    monkeypatch.setattr(b.httpx, "AsyncClient",
                        lambda timeout=None: cap.client(cap))
    return cap


async def test_disable_thinking_adds_template_kwarg(capture):
    be = LlamaCppBackend({"endpoint": "http://x:8084", "disable_thinking": True})
    await be.complete([{"role": "user", "content": "hi"}], {"type": "object"})
    assert capture.sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert capture.sent["response_format"]["type"] == "json_schema"


async def test_default_request_unchanged(capture):
    be = LlamaCppBackend({"endpoint": "http://x:8081"})
    await be.complete([{"role": "user", "content": "hi"}], {"type": "object"})
    assert "chat_template_kwargs" not in capture.sent   # analyst slot untouched


def test_slotmanager_passes_flag_from_slot_cfg():
    sm = SlotManager({"narrative": {"max_tokens": 900},
                      "heavy": {}})
    be = sm._backend({"endpoint": "http://x:8084",
                      "model_id": "heavy", "disable_thinking": True})
    assert be.disable_thinking is True
    be2 = sm._backend({"endpoint": "http://x:8081", "model_id": "analyst"})
    assert be2.disable_thinking is False
