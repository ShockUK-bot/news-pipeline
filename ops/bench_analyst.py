#!/usr/bin/env python3
"""bench_analyst.py — A/B a candidate analyst model against the live one.

Deliberately STANDALONE: it imports nothing from src/. It talks to Postgres
and to llama-server directly, so it cannot be broken by, and cannot break,
the running pipeline. Read-only against the database.

What it measures, per endpoint:
  * wall-clock latency per call (p50 / p95 / max)
  * decode rate (completion tokens / sec)
  * empty-content rate  -- the v0.5.8 / v0.12.4 failure mode
  * JSON-parse rate     -- did strict schema actually bind
  * schema-conform rate -- required keys present, enums respected
  * expected_move_window violations -- the exact defect that cost A2 ~10%
    of its morning throughput on 2026-08-19 (v0.13.8)

Prompts are built from REAL news items the analyst actually processed, so
the input length distribution is representative. The schema below is a
faithful proxy of A2's thesis contract, not a copy of it — this bench
answers "is the new model faster and does it stay on contract", and the
live journal answers "are the theses better" after cutover.

Usage (on the Spark):

    export PIPELINE_DSN='postgresql://trader:trader_dev@127.0.0.1:5432/trading'
    python3 ops/bench_analyst.py \
        --a http://127.0.0.1:8081 --a-name qwen3.6-27b-q5_k_m \
        --b http://127.0.0.1:8082 --b-name qwen3.8-27b-q5_k_m \
        --n 20

Exit code is 0 always; this is a measurement tool, not a gate.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

try:
    import httpx
except ImportError:
    sys.exit("httpx not installed. Run: pip install httpx")

try:
    import psycopg
except ImportError:
    sys.exit("psycopg not installed. Run: pip install 'psycopg[binary]'")


# --------------------------------------------------------------------------
# The contract we hold both models to. Mirrors the shape of A2's thesis:
# nested objects, enums, and the expected_move_window field whose formatting
# was responsible for six wasted retries in one 40-minute window on 08-19.
# --------------------------------------------------------------------------
THESIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ticker", "direction", "horizon", "magnitude_est",
                 "expected_move_window", "confidence", "reasoning",
                 "invalidation"],
    "properties": {
        "ticker": {"type": "string"},
        "direction": {"type": "string", "enum": ["UP", "DOWN", "NONE"]},
        "horizon": {"type": "string",
                    "enum": ["intraday", "multi_day", "multi_week"]},
        "magnitude_est": {"type": "number"},
        "expected_move_window": {
            "type": "string",
            "enum": ["45_minutes", "120_minutes", "1_sessions", "2_sessions",
                     "3_sessions", "1_weeks", "2_weeks", "3_weeks"],
        },
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "invalidation": {
            "type": "object",
            "additionalProperties": False,
            "required": ["machine_checkable", "news_checkable"],
            "properties": {
                "machine_checkable": {"type": "array",
                                      "items": {"type": "string"}},
                "news_checkable": {"type": "array",
                                   "items": {"type": "string"}},
            },
        },
    },
}

VALID_WINDOWS = set(THESIS_SCHEMA["properties"]["expected_move_window"]["enum"])
VALID_DIRECTIONS = {"UP", "DOWN", "NONE"}
VALID_HORIZONS = {"intraday", "multi_day", "multi_week"}

SYSTEM_PROMPT = (
    "You are an equity analyst in an automated trading pipeline. You are given "
    "one news item about a US-listed company. Produce a single trading thesis "
    "as JSON matching the supplied schema, and nothing else.\n"
    "Rules: magnitude_est is the expected absolute price move as a decimal "
    "fraction (0.03 = 3%). confidence is 0.0-1.0. expected_move_window MUST be "
    "one of the exact enum strings — never '60 minutes', never '1_hour'. "
    "machine_checkable invalidations are price/volume conditions; "
    "news_checkable invalidations are follow-up events that would kill the "
    "thesis. Be concise: reasoning is at most three sentences."
)

FETCH_SQL = """
SELECT n.headline, n.summary, n.source, n.source_tier, n.symbols
FROM journal.decisions d
JOIN news.news_items n
  ON n.item_id = d.item_id AND n.revision = COALESCE(d.revision, n.revision)
WHERE d.stage = 'ANALYST'
  AND d.ts > now() - interval '14 days'
  AND n.headline IS NOT NULL
ORDER BY d.ts DESC
LIMIT %s
"""

FALLBACK_SQL = """
SELECT headline, summary, source, source_tier, symbols
FROM news.news_items
WHERE headline IS NOT NULL
  AND received_ts > now() - interval '14 days'
ORDER BY received_ts DESC
LIMIT %s
"""


def load_items(dsn: str, n: int) -> list[dict]:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(FETCH_SQL, (n,))
            rows = cur.fetchall()
            if len(rows) < n:
                print(f"  (only {len(rows)} analysed items in 14d; "
                      f"topping up from the raw feed)")
                cur.execute(FALLBACK_SQL, (n,))
                rows = cur.fetchall()
    items = []
    for headline, summary, source, tier, symbols in rows[:n]:
        items.append({
            "headline": headline,
            "summary": (summary or "")[:2000],
            "source": source,
            "tier": tier,
            "symbols": list(symbols or []),
        })
    return items


def build_messages(item: dict) -> list[dict]:
    ticker = item["symbols"][0] if item["symbols"] else "UNKNOWN"
    user = (
        f"TICKER: {ticker}\n"
        f"SOURCE: {item['source']} (trust tier {item['tier']})\n"
        f"HEADLINE: {item['headline']}\n"
        f"BODY: {item['summary'] or '(no body text supplied)'}\n\n"
        "Produce the thesis JSON now."
    )
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


async def one_call(client: httpx.AsyncClient, endpoint: str, model_id: str,
                   messages: list[dict], max_tokens: int,
                   no_think: bool) -> dict:
    t0 = time.monotonic()
    result = {"ok": False, "latency_s": None, "tokens": None, "tps": None,
              "empty": False, "json_ok": False, "schema_ok": False,
              "window_bad": False, "magnitude_bad": False, "reasoning_chars": 0,
              "error": None, "content": ""}
    body = {
        "model": model_id,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "thesis", "strict": True,
                            "schema": THESIS_SCHEMA},
        },
    }
    if no_think:
        # Proven on the Spark 2026-08-21: this is the ONLY lever that stops
        # Qwen3.8 thinking. --reasoning-budget 0 is ignored (same as the 122B
        # in v0.12.4); --reasoning-effort low reduces but does not stop it.
        # With thinking on, a thesis costs 435 tokens / 25.8s; with it off,
        # 78 tokens / 4.9s. Mirrors LlamaCppBackend's disable_thinking flag.
        body["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        resp = await client.post(
            f"{endpoint.rstrip('/')}/v1/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - a bench should report, not crash
        result["latency_s"] = time.monotonic() - t0
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return result

    result["latency_s"] = time.monotonic() - t0
    result["ok"] = True

    msg = (data.get("choices") or [{}])[0].get("message") or {}
    result["reasoning_chars"] = len(msg.get("reasoning_content") or "")
    content = (msg.get("content") or "").strip()
    # Same read-path as src/a1_triage/backends.py since v0.11.9.
    if not content:
        result["empty"] = True
        content = (msg.get("reasoning_content") or "").strip()
    result["content"] = content

    usage = data.get("usage") or {}
    tokens = usage.get("completion_tokens")
    if isinstance(tokens, int) and tokens > 0:
        result["tokens"] = tokens
        result["tps"] = tokens / max(result["latency_s"], 1e-6)

    try:
        parsed = json.loads(content)
    except Exception:
        return result
    result["json_ok"] = True

    required = set(THESIS_SCHEMA["required"])
    if not isinstance(parsed, dict) or not required.issubset(parsed):
        return result
    if parsed.get("direction") not in VALID_DIRECTIONS:
        return result
    if parsed.get("horizon") not in VALID_HORIZONS:
        return result
    inval = parsed.get("invalidation")
    if not isinstance(inval, dict) or "machine_checkable" not in inval:
        return result
    if parsed.get("expected_move_window") not in VALID_WINDOWS:
        result["window_bad"] = True
        return result
    # Unit-convention check. magnitude_est is a DECIMAL FRACTION (0.03 = 3%).
    # A value above 1.0 means the model answered in percent — a 100x sizing
    # error if it ever reached A3. Schema-valid, semantically catastrophic,
    # and exactly the kind of care a model stops taking when thinking is off.
    mag = parsed.get("magnitude_est")
    if not isinstance(mag, (int, float)) or not (0.0 <= float(mag) <= 1.0):
        result["magnitude_bad"] = True
        return result
    result["schema_ok"] = True
    return result


async def bench(name: str, endpoint: str, model_id: str, items: list[dict],
                max_tokens: int, timeout: float, no_think: bool) -> dict:
    print(f"\n--- {name}  ({endpoint})  thinking="
          f"{'OFF (per-request)' if no_think else 'server default'} ---")
    results = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for i, item in enumerate(items, 1):
            r = await one_call(client, endpoint, model_id,
                               build_messages(item), max_tokens, no_think)
            results.append(r)
            mark = ("ERR " if r["error"] else
                    "PASS" if r["schema_ok"] else
                    "WIN!" if r["window_bad"] else
                    "MAG!" if r["magnitude_bad"] else
                    "BAD ")
            tps = f"{r['tps']:6.1f} tok/s" if r["tps"] else "    -  tok/s"
            print(f"  [{i:>3}/{len(items)}] {mark} "
                  f"{r['latency_s']:6.2f}s  {tps}  "
                  f"{item['headline'][:56]}")
            if r["error"]:
                print(f"        -> {r['error']}")
    return summarise(name, endpoint, model_id, results)


def summarise(name: str, endpoint: str, model_id: str,
              results: list[dict]) -> dict:
    n = len(results)
    lats = sorted(r["latency_s"] for r in results if r["latency_s"] is not None)
    tpss = [r["tps"] for r in results if r["tps"]]

    def pct(p: float) -> float:
        if not lats:
            return 0.0
        k = min(len(lats) - 1, int(round(p * (len(lats) - 1))))
        return lats[k]

    return {
        "name": name, "endpoint": endpoint, "model_id": model_id, "n": n,
        "errors": sum(1 for r in results if r["error"]),
        "p50": pct(0.50), "p95": pct(0.95),
        "max": max(lats) if lats else 0.0,
        "mean_tps": statistics.mean(tpss) if tpss else 0.0,
        "empty": sum(1 for r in results if r["empty"]),
        "json_ok": sum(1 for r in results if r["json_ok"]),
        "schema_ok": sum(1 for r in results if r["schema_ok"]),
        "window_bad": sum(1 for r in results if r["window_bad"]),
        "magnitude_bad": sum(1 for r in results if r["magnitude_bad"]),
        "mean_out_tokens": (statistics.mean([r["tokens"] for r in results
                                             if r["tokens"]])
                            if any(r["tokens"] for r in results) else 0.0),
        "mean_reasoning": statistics.mean([r["reasoning_chars"]
                                           for r in results]) if results else 0.0,
    }


def report(rows: list[dict]) -> None:
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    hdr = (f"{'slot':<22}{'p50':>8}{'p95':>8}{'tok/s':>8}{'out tok':>9}"
           f"{'schema':>9}{'empty':>7}{'errs':>6}")
    print(hdr)
    print("-" * 78)
    for r in rows:
        schema = f"{r['schema_ok']}/{r['n']}"
        print(f"{r['name']:<22}{r['p50']:>7.2f}s{r['p95']:>7.2f}s"
              f"{r['mean_tps']:>8.1f}{r['mean_out_tokens']:>9.0f}{schema:>9}"
              f"{r['empty']:>7}{r['errors']:>6}")
    print("-" * 78)
    print("  p50 latency is the decision metric. tok/s is diagnostic only:")
    print("  a model decoding 2x faster while emitting 3x the tokens is SLOWER.")
    for r in rows:
        if r["mean_reasoning"] > 5:
            print(f"  {r['name']}: averaging {r['mean_reasoning']:.0f} chars of "
                  f"reasoning per call — thinking is NOT fully suppressed.")
        if r["window_bad"]:
            print(f"  {r['name']}: {r['window_bad']} expected_move_window "
                  f"violation(s) — the v0.13.8 defect, still present.")
        if r["magnitude_bad"]:
            print(f"  {r['name']}: {r['magnitude_bad']} magnitude_est value(s) "
                  f"outside 0.0-1.0 — PERCENT/FRACTION CONFUSION. This is a "
                  f"100x sizing error at A3. Hard NO-GO.")
    if len(rows) == 2:
        a, b = rows
        if a["p50"] and b["p50"]:
            delta = (a["p50"] - b["p50"]) / a["p50"] * 100
            verb = "FASTER" if delta > 0 else "SLOWER"
            print(f"\n  {b['name']} is {abs(delta):.0f}% {verb} than "
                  f"{a['name']} at p50 latency.")
        print(f"  Contract adherence: {a['name']} {a['schema_ok']}/{a['n']}, "
              f"{b['name']} {b['schema_ok']}/{b['n']}.")
    print("\nGO criteria for cutover (all four must hold):")
    print("  1. candidate p50 <= live p50            [wall clock, not tok/s]")
    print("  2. candidate schema_ok >= live schema_ok")
    print("  3. candidate empty == 0 and errors == 0")
    print("  4. candidate magnitude_bad == 0         [non-negotiable]")
    print()


async def main() -> None:
    ap = argparse.ArgumentParser(description="A/B two analyst model slots.")
    ap.add_argument("--a", default="http://127.0.0.1:8081",
                    help="live endpoint (baseline)")
    ap.add_argument("--a-name", default="qwen3.6-27b-q5_k_m")
    ap.add_argument("--b", default="http://127.0.0.1:8082",
                    help="candidate endpoint")
    ap.add_argument("--b-name", default="qwen3.8-27b-q5_k_m")
    ap.add_argument("--n", type=int, default=20, help="news items to replay")
    ap.add_argument("--max-tokens", type=int, default=1200,
                    help="matches config/a2.yaml model.max_tokens")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--only", choices=["a", "b"], default=None,
                    help="bench a single endpoint")
    ap.add_argument("--a-think", action="store_true",
                    help="let endpoint A think (default: A relies on its "
                         "unit-level suppression, so no kwarg is sent)")
    ap.add_argument("--b-think", action="store_true",
                    help="let endpoint B think. Default is OFF: Qwen3.8 only "
                         "stops thinking via the per-request kwarg.")
    args = ap.parse_args()

    dsn = os.environ.get("PIPELINE_DSN")
    if not dsn:
        sys.exit("PIPELINE_DSN is not set. See ops/RUNBOOK.md.")

    print(f"Loading {args.n} recent news items from the journal...")
    items = load_items(dsn, args.n)
    if not items:
        sys.exit("No news items found. Is the pipeline running?")
    print(f"Loaded {len(items)} items.")

    rows = []
    if args.only != "b":
        rows.append(await bench(args.a_name, args.a, args.a_name, items,
                                args.max_tokens, args.timeout,
                                no_think=False))
    if args.only != "a":
        rows.append(await bench(args.b_name, args.b, args.b_name, items,
                                args.max_tokens, args.timeout,
                                no_think=not args.b_think))
    report(rows)


if __name__ == "__main__":
    asyncio.run(main())
