# v0.14.0 — Qwen3.8-27B shadow analyst slot + analyst A/B bench (2026-08-21)

**Additive only. Zero replaced files. Nothing the running pipeline touches
changes.** This release stands a candidate analyst model up on a spare port
and gives you a tool that produces the numbers to decide with. The cutover
is a separate, deliberate step (Part 7 of the deploy guide), gated on those
numbers.

## Why now

The analyst slot is the system's binding constraint, and it has been for
three weeks of incident reviews:

- 2026-08-05: A2 throughput ~40 s/signal; worst queue waits ~15 min.
- 2026-08-19: six scanner signals waited 24–26 minutes; three died
  `SCANNER_STALE` on moves that kept running. A2's own model latency was
  ~40 s each — the queue was the killer, but the queue is only as fast as
  the model draining it.
- 2026-08-19 (Finding 5): six `expected_move_window` schema violations in
  one 40-minute window, each burning a full ~30–50 s retry — roughly 10% of
  morning throughput, on the exact queue that was starving the scanner lane.

v0.12.15 (late-pass pacing), v0.13.8 (scanner priority 10, `coerce_window`)
and the A4 capacity work all attack the queue. None of them make the model
faster. This does.

## The candidate: Qwen3.8-27B

Alibaba published it on 2026-08-14, Apache 2.0, dense 27B — the direct
successor to the deployed Qwen3.6-27B, same size class, same family, so
the swap is a model path and a `model_id` string.

Three properties matter here, and only one of them is a benchmark:

1. **MTP (Multi-Token Prediction) layers ship inside the GGUF.** They act as
   a built-in draft model, so llama.cpp can run speculative decoding without
   a second model in memory. This is the direct attack on 12.5 tok/s.
2. **`reasoning_effort` is a server flag.** Qwen3.8 defaults to `xhigh` —
   maximum reasoning on every request. Turned down, community measurements
   report multiples of throughput. Our analyst prompts do not need
   deliberation; they need contract adherence at speed.
3. **3:1 hybrid attention** — only 16 of 64 layers keep a KV cache, so 32K
   context costs roughly 2 GB instead of ~8 GB.

Vendor evals (Alibaba's own, unverified independently) put it above
Qwen3.6-27B: SWE-bench Pro 61.7 vs 53.5, agentic terminal coding 73.0 vs
63.4. Artificial Analysis scores it 52 on its Intelligence Index against 38
for the predecessor. **We are not deploying on those numbers.** We are
deploying on `ops/bench_analyst.py` output from this machine.

## What is in the pack

**NEW:**

- `ops/systemd/llama-a2b.service` — shadow analyst slot, Qwen3.8-27B on
  **:8082**, `Restart=no`, no boot enable, manual start only. Nothing in the
  pipeline calls :8082.
- `ops/bench_analyst.py` — standalone A/B harness. Imports nothing from
  `src/`; read-only against Postgres. Replays real recent news items through
  both endpoints under a strict `json_schema` and reports p50/p95 latency,
  decode rate, empty-content rate, JSON-parse rate, schema conformance, and
  `expected_move_window` violations specifically.
- `claude/patch-notes-v0_14_0.md`, `claude/v0_14_0-deploy-guide.md`.

**REPLACED:** none.
**Migrations:** none.
**Tests:** unaffected — no `src/` file is touched, so the release set is
unchanged. `ops/bench_analyst.py` is an operator tool, not pipeline code.

## Three unit-file decisions, and why

The new unit is deliberately **not** a copy of `llama-a2.service`:

- **`--jinja` added.** Qwen3.8's reasoning-effort control lives in the
  model's own chat template. Without `--jinja`, llama-server substitutes a
  generic template and `--reasoning-effort` silently does nothing.
- **`--reasoning-effort low` added.** Only `low`, `medium`, `xhigh` are
  valid for this template. llama.cpp *also* accepts `minimal`/`high`/`max`,
  but this model's template throws on them — and the failure shape is the
  dangerous one: **the server starts healthy and then fails every single
  request.** Part 3 of the deploy guide exists to catch exactly that before
  a unit file is ever installed.
- **`--grammar-file` dropped, `--chat-template-kwargs` dropped.** The
  server-wide JSON grammar is what stopped Qwen3.5/3.6 from ever closing
  their `<think>` block (v0.5.8), and v0.11.9 already flagged it as
  redundant with the per-request strict schema. Qwen3.8 controls thinking
  via `reasoning_effort`, not `enable_thinking`, and passing an unknown
  template kwarg risks a template error. The bench sends strict schemas, so
  this slot is also the experiment that tells us whether the server-wide
  grammar is still needed at all.

This is the fourth time this model family's thinking channel has been the
blocker (v0.4.3, v0.5.8, v0.11.9, v0.12.4). Part 3 is not optional.

## Build strategy: out-of-tree llama.cpp

Revised after the first Part 1 attempt on the Spark hit two permission
failures. `/opt/llama.cpp` was root-owned from the original `sudo` install,
so `git pull` refused ("dubious ownership") and cmake could not write into
`build/`. The build aborted at the configure stage — before `llama-server`
was relinked — so the live binary was never at risk. But it exposed that the
old plan was rebuilding **in place, over the binary serving live trades**.

The guide now takes ownership of `/opt/llama.cpp` once and builds into
`/opt/llama.cpp/build-2026-08/`, leaving `/opt/llama.cpp/build/` intact.
Consequences:

- Part 1 is genuinely safe during market hours — the running binary is not
  touched, not just "already loaded into memory".
- llama.cpp gets a real rollback: change the `ExecStart` path back.
- The cutover (Part 7) moves the analyst service onto the new binary and the
  new model in the same `sed`, so they roll back together. `llama-a1` and
  `llama-heavy` stay on the old binary — one changed service at a time.

## Measured on the Spark, 2026-08-21 (pre-bench hand tests)

llama.cpp b10573, `unsloth/Qwen3.8-27B-GGUF` UD-Q5_K_M, one thesis-shaped
request under a strict `json_schema`:

| Configuration | Reasoning | Output | Wall |
|---|---|---|---|
| `--reasoning-effort low` | 792 chars | 435 tok | 25.8 s |
| plus `--reasoning-budget 0` | 792 chars | 435 tok | 25.8 s |
| per-request `enable_thinking: false` | 0 chars | 78 tok | **4.9 s** |

Raw decode with `--spec-type draft-mtp`: 11.3 → 21.3 tok/s. Under the strict
grammar, ~16 tok/s — the grammar rejects draft tokens, which is expected.

**Three findings that changed this release:**

1. **`--reasoning-budget 0` is ignored by Qwen3.8**, identical to the 122B in
   v0.12.4. The unit file therefore does NOT try to suppress thinking; the
   cutover uses `disable_thinking: true` in config, driving the
   `LlamaCppBackend` flag that has existed since v0.12.4. **No code change.**
2. **Dropping `--grammar-file` was correct.** A `json_object` request came
   back fenced with a markdown code block; the strict `json_schema` the
   pipeline actually sends returned bare valid JSON with correct enums.
   Server-wide grammar is not needed on b10573.
3. **tok/s is a trap.** The candidate decodes ~1.9× faster and, with thinking
   on, emits ~4.7× the tokens — net slower than the model it replaces. The
   bench and the GO criteria were rewritten around **p50 wall clock**.

## Bench changes off the back of it

- Sends `chat_template_kwargs {"enable_thinking": false}` to the candidate by
  default (`--b-think` to override), mirroring the production config path.
- Reports mean output tokens and mean reasoning chars per call, so a
  half-suppressed thinking channel is visible rather than inferred.
- **New hard NO-GO: `magnitude_est` outside 0.0–1.0.** A hand test with a
  prompt that did not state units returned `8.5` — percent instead of
  decimal fraction, a 100× sizing error at A3. Schema-valid, semantically
  catastrophic, and precisely the care a model stops taking when thinking is
  switched off. The bench states the unit convention in its system prompt
  and then checks the model honoured it.

## Rollback

Stop the unit. There is nothing else to undo — no config, no schema, no
service the pipeline depends on. `git checkout v0.13.10` restores the repo
if you want the files gone too.

## Deferred: Nemotron 3 Super in the heavy slot

Scoped, not built. See `claude/v0_14_0-deploy-guide.md` "Appendix — Nemotron
3 Super". Short version: it fits (~65–70 GB at Q4_K_M vs the current 122B's
77 GB), it runs on upstream llama.cpp on GB10, and it is NVIDIA's own
Spark-targeted agentic model. But A4's ranking failure is a
`narrative.max_tokens: 1400` config bug, not a model-quality problem, and
swapping the heavy model would not fix it. Fix the cap first, then
re-measure. It also arrives under the NVIDIA Open Model License rather than
Apache 2.0 / MIT, which is the first non-permissive licence in the loadout
and deserves a deliberate decision rather than a drive-by upgrade.
