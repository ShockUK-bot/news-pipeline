"""Model backends for A1. The pipeline sees one interface; the model behind it
is a config line.

LlamaCppBackend: llama-server's OpenAI-compatible /v1/chat/completions with
  response_format json_schema — the grammar constraint is enforced server-side
  during decoding, so off-contract tokens can't be sampled. Code-side
  validation still runs (spec §13). Smoke-tested on the Spark, not here (no
  model server in the build environment).

StubBackend: scripted responses for tests and Spark-less dev. Deterministic:
  pops from a queue of canned responses, or applies a simple keyword rule.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from common.log import get_logger, kv

log = get_logger("a1.backend")


@dataclass
class ModelReply:
    text: str
    latency_ms: int
    model_id: str


def _extract_text(message: dict) -> str:
    """Pull the model's answer out of an OpenAI-compatible chat message.

    Normally it's in ``content``. But a reasoning model served by llama.cpp can
    route the *entire* answer into ``reasoning_content`` and leave ``content``
    empty — this is what the heavy off-hours slot (Qwen3.5-122B-A10B on :8084)
    does: the thinking template opens an implicit ``<think>`` block, the
    schema-constrained JSON is generated inside it, and because the grammar
    never lets the model emit a closing ``</think>``, llama.cpp's deepseek-style
    reasoning parser classifies the whole (still perfectly valid) JSON as
    thinking and hands back ``content == ""``. A4/A5 then saw an empty string,
    failed schema validation, and fell back to deterministic ranking with the
    "primary model unavailable" briefing line.

    Since ``response_format`` is ``strict``, whatever the server returns is
    schema-valid JSON no matter which field it lands in, so we prefer
    ``content`` and fall back to ``reasoning_content`` when content is blank.
    Downstream ``validate_*`` still guards against anything malformed, so a bad
    read degrades to exactly the old behaviour rather than misparsing. (v0.11.9)
    """
    text = (message.get("content") or "").strip()
    if text:
        return text
    return (message.get("reasoning_content") or "").strip()


class ModelBackend(Protocol):
    model_id: str
    async def complete(self, messages: list[dict], json_schema: dict) -> ModelReply: ...


class LlamaCppBackend:
    def __init__(self, cfg: dict):
        self.endpoint = cfg["endpoint"].rstrip("/")
        self.model_id = cfg.get("model_id", "unknown")
        self.temperature = float(cfg.get("temperature", 0.0))
        self.max_tokens = int(cfg.get("max_tokens", 512))
        self.timeout = float(cfg.get("timeout_secs", 30))
        # v0.12.4 — disable the model's thinking phase at the CHAT-TEMPLATE
        # level, per request. Probe evidence (2026-07-24, heavy slot): with
        # thinking on, the 122B narrates prose inside its <think> block, the
        # response_format grammar never bites (it constrains content only),
        # generation burns the whole max_tokens budget in reasoning and
        # finishes on `length` with content == "" — every heavy consumer
        # (A4 sheet, A6 nightly, A7/A8 narrative) then falls back. With
        # chat_template_kwargs {"enable_thinking": false} the same request
        # returned schema-valid JSON in `content` at 52 tokens, finish=stop.
        # (Server-side flags — --reasoning-budget 0 is IN the unit — are
        # demonstrably ignored by this build; the /no_think soft switch is
        # ignored by this template. The per-request kwarg is what works.)
        self.disable_thinking = bool(cfg.get("disable_thinking", False))

    async def complete(self, messages: list[dict], json_schema: dict) -> ModelReply:
        t0 = time.monotonic()
        body = {
            "model": self.model_id,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "triage", "strict": True,
                                "schema": json_schema},
            },
        }
        if self.disable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.endpoint}/v1/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
        latency = int((time.monotonic() - t0) * 1000)
        text = _extract_text(data["choices"][0]["message"])
        return ModelReply(text=text, latency_ms=latency, model_id=self.model_id)


class StubBackend:
    """Test/dev backend. Two modes:
    * scripted: pass a list of raw response strings, popped in order;
    * rule-based fallback: material iff the headline contains a trigger word,
      first feed symbol as ticker — enough to drive the pipeline end-to-end.
    """
    TRIGGERS = ("acquisition", "merger", "fda", "earnings", "guidance",
                "buyback", "bankruptcy", "recall", "contract", "resigns")

    def __init__(self, scripted: list[str] | None = None,
                 model_id: str = "stub-0"):
        self.scripted = list(scripted or [])
        self.model_id = model_id
        self.calls: list[list[dict]] = []       # recorded for test assertions

    async def complete(self, messages: list[dict], json_schema: dict) -> ModelReply:
        self.calls.append(messages)
        if self.scripted:
            return ModelReply(self.scripted.pop(0), latency_ms=1, model_id=self.model_id)
        item = json.loads(messages[-1]["content"].split("\n\n")[0])
        headline = (item.get("headline") or "").lower()
        material = any(t in headline for t in self.TRIGGERS)
        out = {
            "material": material,
            "tickers": item.get("symbols", [])[:1] if material else [],
            "direction_hint": "unclear",
            "urgency": "medium" if material else "low",
            "novelty_score": 0.8 if item.get("is_new_story") else 0.3,
            "confidence": 0.9 if material else 0.8,
            "reason": "stub rule: trigger word match" if material else "stub rule: no trigger",
        }
        return ModelReply(json.dumps(out), latency_ms=1, model_id=self.model_id)


def get_backend(cfg: dict) -> ModelBackend:
    kind = cfg.get("backend", "llamacpp")
    if kind == "llamacpp":
        return LlamaCppBackend(cfg)
    if kind == "stub":
        return StubBackend()
    raise RuntimeError(f"unknown model backend: {kind!r}")

