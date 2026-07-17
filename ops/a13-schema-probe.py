"""Probe the Analyst llama-server with A13's two response schemas (v0.5.5).

Run on the Spark after any schema change, BEFORE restarting a13-chat:

    sudo -u trader bash -c 'cd /opt/pipeline && PYTHONPATH=src \
        .venv/bin/python ops/a13-schema-probe.py'

Both probes must print HTTP 200. A 400 "failed to parse grammar" means the
schema contains a construct this llama.cpp build cannot convert (observed:
nullable sub-objects / anyOf-with-null) — fix the schema, don't deploy.
"""
from __future__ import annotations

import asyncio
import os

import httpx

from a13_chat.schema import answer_json_schema, planner_json_schema

ENDPOINT = os.environ.get("A13_PROBE_ENDPOINT", "http://127.0.0.1:8081")


async def probe(name: str, schema: dict) -> bool:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{ENDPOINT}/v1/chat/completions", json={
            "model": "probe",
            "messages": [{"role": "user", "content": "say hi as json"}],
            "max_tokens": 20,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "probe", "strict": True,
                                                "schema": schema}}})
        ok = r.status_code == 200
        print(f"{name}: HTTP {r.status_code} {'OK' if ok else r.text[:300]}")
        return ok


async def main() -> None:
    ok1 = await probe("planner", planner_json_schema())
    ok2 = await probe("answer", answer_json_schema())
    raise SystemExit(0 if (ok1 and ok2) else 1)


if __name__ == "__main__":
    asyncio.run(main())
