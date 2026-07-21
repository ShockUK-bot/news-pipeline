# v0.11.9 — Heavy model: read the answer out of `reasoning_content`

**One file changed. No trading-logic changes. No model restart, no systemd
change, no database change.** This fixes the "primary model unavailable /
deterministic fallback mode" line you saw in the 2026-07-21 morning briefing.

## What was actually wrong

The heavy off-hours model (`Qwen3.5-122B-A10B` on `:8084`) loads fine since
v0.11.8b — but every time A4 (pre-market ranking, 07:00), A5 (thematic digest,
21:30) or A7 (reports) called it, it came back **empty**, so those agents threw
the answer away and fell back to their deterministic, no-LLM path. That's what
"primary model unavailable" meant: not that the model was down, but that the
pipeline couldn't read what it said.

Here's the chain, start to finish:

1. Our model client (`src/a1_triage/backends.py`) sends the request with
   `response_format: json_schema, strict: true`, then reads the reply out of
   the message's **`content`** field — the normal place an answer lives.
2. The heavy model is a *thinking* model. Its chat template opens an implicit
   "thinking" block (`<think>`) at the start of every answer.
3. Our request also forces the output to be valid JSON (the strict schema, plus
   a server-side JSON grammar on the `:8084` unit). So the model emits the JSON
   answer **while it is still inside the thinking block**, and — because the
   grammar only allows JSON tokens — it can never emit the closing `</think>`
   tag that would end the thinking block.
4. llama.cpp's reasoning parser therefore decides the **entire** answer is
   "thinking", and hands it back in a *different* field, `reasoning_content`,
   leaving `content` empty.
5. Our client only looked at `content`. It saw `""`, failed schema validation,
   retried, failed again, and dropped to the deterministic fallback.

We proved step 4 directly off-hours: an unconstrained probe of `:8084` returned
`{"ok": true}` — perfectly valid JSON — sitting in `reasoning_content`, with
`content` empty and the model reporting a normal, complete finish. The model was
never broken. We were reading the wrong field.

(Earlier we tried to stop the model from "thinking" at the server with
`--reasoning-budget 0` and `enable_thinking:false`; on this build/model those
flags didn't take, and swapping in `--reasoning-format none` crashed sampler
init because it collides with the server-side grammar. Chasing that is fragile
and build-specific. Reading the field the model actually uses is not — it's the
same fix everyone serving reasoning models locally ends up making.)

## The fix

`src/a1_triage/backends.py` now pulls the answer out with a small helper,
`_extract_text(message)`:

- Prefer `content` (normal slots — the 9B triage and 27B analyst — are
  unaffected; they put the answer in `content` as before).
- If `content` is blank, fall back to `reasoning_content` (the heavy slot).

Because the request is `strict`, whatever the server returns is schema-valid
JSON **no matter which field it lands in**, so this is safe. And the existing
`validate_sheet` / `validate_thematic` checks still run on the result, so a
genuinely malformed reply degrades to exactly the old behaviour (retry, then
deterministic fallback) rather than being mis-parsed.

## Files in this pack

- **REPLACED:** `src/a1_triage/backends.py` — the `_extract_text` helper +
  wiring it into `LlamaCppBackend.complete`.
- **NEW:** `tests/unit/test_backend_reasoning.py` — 8 unit tests covering the
  content path, the `reasoning_content` fallback, null/whitespace content,
  "both present → prefer content", and the empty-both case.

## Validation

`tests/unit/test_backend_reasoning.py` — **8 passed**. The existing triage
suite (`test_triage_router.py`, `test_triage_v047.py`) — **35 passed**,
unchanged by this edit. (`test_triage_v047.py::test_confidence_required` fails
both before and after this change — a pre-existing, unrelated assertion about
schema-error wording; noted for a future tidy, not touched here.)

The real proof is the next off-hours heavy run: tonight's **21:30 A5 thematic**
and tomorrow's **07:00 A4 pre-market** should produce real model output instead
of deterministic fallback, and the **2026-07-22 morning briefing should no
longer say "primary model unavailable / deterministic fallback mode."**

## Deploy

Pure library code. Pull the tag; nothing needs an immediate restart. The
heavy-slot agents (A4, A5, A7) start a fresh process on their next scheduled run
and pick up the new code automatically. See `v0_11_9-deploy-guide.md`.

## Rollback

`git checkout v0.11.8` and, if you restarted any always-on agents, restart them
again. No database or systemd changes were made, so there's nothing else to
undo.

## Optional follow-up (not needed now)

The `:8084` unit still carries a server-wide `--grammar-file` that is redundant
with the per-request `strict` schema and is the eager grammar behind the
reasoning-routing quirk. With this fix in place it's harmless. If we ever want
the model to put the answer back in `content` at the source, the clean change is
to drop `--grammar-file` from `ops/systemd/llama-heavy.service` and validate
one off-hours run — a separate, optional tidy.
