"""Cold-start pre-flight (soak-kit v1.0). Run BEFORE RUNBOOK section 6, every time.

Verifies everything the cold-start order assumes, in dependency order:
env -> Postgres (schemas, control keys) -> Qdrant -> llama slots (:8080/:8081,
grammar-constrained JSON round-trip) -> optional broker auth.

Usage:
    PYTHONPATH=src python3 ops/preflight.py            # infra only
    PYTHONPATH=src python3 ops/preflight.py --broker   # + Alpaca auth check

Exit 0 = all PASS. Exit 1 = at least one FAIL (do not cold-start).
Read-only except the broker check, which is auth + account read only.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

REQUIRED_ENV = ["PIPELINE_DSN", "ALPACA_KEY_ID", "ALPACA_SECRET_KEY", "EDGAR_CONTACT"]
REQUIRED_SCHEMAS = {"news", "queue", "journal"}
REQUIRED_CONTROL_KEYS = {"kill_switch", "drawdown_breaker", "block_entries",
                         "trading_capital", "max_trades_per_day"}
LLAMA_SLOTS = [("llama-a1 (Fast/triage)", "http://127.0.0.1:8080"),
               ("llama-a2 (Analyst)", "http://127.0.0.1:8081")]

results = []


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def check_env():
    for var in REQUIRED_ENV:
        record(f"env {var}", bool(os.environ.get(var)), "" if os.environ.get(var) else "unset/empty")
    emb = os.environ.get("EMBEDDER", "hash")
    record("env EMBEDDER=bge (production)", emb == "bge", f"EMBEDDER={emb!r}")
    for var, prod in (("BROKER", "alpaca"), ("MARKETDATA", "alpaca")):
        val = os.environ.get(var, prod)
        record(f"env {var} is production", val == prod,
               f"{var}={val!r}" + ("" if val == prod else " - test residue? unset it"))


def check_postgres():
    try:
        import psycopg
        with psycopg.connect(os.environ["PIPELINE_DSN"], connect_timeout=5) as conn:
            ver = conn.execute("SHOW server_version").fetchone()[0]
            record("postgres reachable", True, f"server {ver}")
            got = {r[0] for r in conn.execute(
                "SELECT schema_name FROM information_schema.schemata")}
            missing = REQUIRED_SCHEMAS - got
            record("schemas news/queue/journal", not missing,
                   f"missing: {sorted(missing)}" if missing else "")
            if "journal" in got:
                keys = {r[0] for r in conn.execute("SELECT key FROM journal.control")}
                miss = REQUIRED_CONTROL_KEYS - keys
                record("journal.control seeded", not miss,
                       f"missing keys: {sorted(miss)} (run sql/control-init.sql)" if miss else "")
                for k in ("kill_switch", "drawdown_breaker", "block_entries"):
                    if k in keys:
                        v = conn.execute("SELECT value FROM journal.control WHERE key=%s",
                                         (k,)).fetchone()[0]
                        record(f"control {k}=0", v == "0",
                               f"{k}={v!r}" + ("" if v == "0" else " - operator-set? see RUNBOOK section 5"))
    except Exception as exc:
        record("postgres reachable", False, str(exc)[:120])


async def check_qdrant():
    import httpx
    url = os.environ.get("QDRANT_URL") or "http://127.0.0.1:6333"
    try:
        # trust_env=False: loopback services must never route via proxy env
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            r = await client.get(f"{url}/readyz")
            record("qdrant ready", r.status_code == 200, f"{url} -> {r.status_code}")
    except Exception as exc:
        record("qdrant ready", False, f"{url}: {str(exc)[:100]}")


async def check_llama():
    import httpx
    body = {"messages": [{"role": "user",
                          "content": 'Reply with exactly this JSON object: {"ok": true}'}],
            "response_format": {"type": "json_object"}, "max_tokens": 64,
            "temperature": 0}
    for name, url in LLAMA_SLOTS:
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                r = await client.post(f"{url}/v1/chat/completions", json=body)
                content = r.json()["choices"][0]["message"]["content"]
                json.loads(content)  # server-side grammar must yield valid JSON
                record(f"{name} JSON round-trip", True, url)
        except Exception as exc:
            record(f"{name} JSON round-trip", False, f"{url}: {str(exc)[:100]}")


async def check_broker():
    try:
        from common.broker import AlpacaBroker
        b = AlpacaBroker()
        acct = await b.get_account()
        record("alpaca paper auth", True,
               f"equity={acct.equity:.2f} settled={acct.settled_cash:.2f}")
    except Exception as exc:
        record("alpaca paper auth", False, str(exc)[:120])


async def main():
    print("pre-flight: env")
    check_env()
    print("pre-flight: postgres")
    check_postgres()
    print("pre-flight: qdrant")
    await check_qdrant()
    print("pre-flight: llama slots")
    await check_llama()
    if "--broker" in sys.argv:
        print("pre-flight: broker")
        await check_broker()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("DO NOT COLD-START. Failing:", ", ".join(failed))
        return 1
    print("PRE-FLIGHT CLEAN - proceed with RUNBOOK section 6.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
