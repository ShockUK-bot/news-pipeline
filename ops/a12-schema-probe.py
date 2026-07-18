"""Probe the analyst llama-server with A12's grammar schema BEFORE deploy
(a13-deploy-guide v1.1 §3 pre-flight gate: every schema a service will send
must return HTTP 200 from the live server — llama.cpp grammar limits are
build-specific and bite at request time, not startup).

Run on the Spark:
  cd /opt/pipeline && PYTHONPATH=src .venv/bin/python ops/a12-schema-probe.py

Every line must end "HTTP 200". A 400 means the grammar failed to compile —
do not start a12-guard; bisect the schema and tell Claude.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from a12_guard.schema import guard_json_schema  # noqa: E402

ENDPOINT = os.environ.get("A12_ENDPOINT", "http://127.0.0.1:8081")


def probe(name: str, schema: dict) -> int:
    req = urllib.request.Request(
        f"{ENDPOINT}/v1/chat/completions",
        data=json.dumps({
            "messages": [{"role": "user", "content": "Reply with JSON."}],
            "max_tokens": 8,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": name, "strict": True,
                                                "schema": schema}},
        }).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


if __name__ == "__main__":
    status = probe("guard_verdict", guard_json_schema())
    print(f"guard_verdict  HTTP {status}")
    sys.exit(0 if status == 200 else 1)
