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

    async def complete(self, messages: list[dict], json_schema: dict) -> ModelReply:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.endpoint}/v1/chat/completions",
                json={
                    "model": self.model_id,
                    "messages": messages,
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "triage", "strict": True,
                                        "schema": json_schema},
                    },
                })
            resp.raise_for_status()
            data = resp.json()
        latency = int((time.monotonic() - t0) * 1000)
        text = data["choices"][0]["message"]["content"]
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
