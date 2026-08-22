# v0.14.1 — Qwen3.8-27B live in the analyst slot (2026-08-22)

Analyst p50 19.6s -> 11.0s (-44%), p95 23.0s -> 12.5s, 20/20 schema, on
llama.cpp b10573 (build-2026-08, out-of-tree; old binary kept for rollback).

- llama-a2.service: new binary, qwen3.8-27b-q5_k_m.gguf, -c 32768, --jinja
  --reasoning-effort low --spec-type draft-mtp; dropped --grammar-file,
  --reasoning-budget, --chat-template-kwargs.
- disable_thinking: true added to every :8081 model block (10 insertions,
  a6 twice). This flag IS the gain: thinking-on 36s/thesis, off 11s.
  --reasoning-budget 0 is ignored by Qwen3.8 (as by the 122B, v0.12.4).
- model_id provenance -> qwen3.8-27b-q5_k_m. a13.yaml had carried
  qwen3-32b-q5_k_m since v0.5.7 (~6 weeks of wrong provenance); fixed.
- bench_analyst.py: journal.decisions has no revision column; original join
  could scan the news_items firehose (froze the box 2026-08-21 under
  model-load memory pressure). Now CTE + LATERAL by PK + 60s
  statement_timeout.

Ops notes: /opt was root-owned (chown + safe.directory applied); drop page
caches before loading 19GB with --no-mmap; restart agents after unclean
boot (DNS/Postgres race); bench --only a sends no thinking kwarg so it
measures server default, not pipeline config — verify via --only b at the
live port and the journal. Outstanding: first organic ANALYST decision
Monday, expect latency_ms ~11000.
