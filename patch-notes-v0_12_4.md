# v0.12.4 — heavy-model narratives fixed (thinking disabled per-request) + A13 wake fix (2026-07-24)

## The bug (finally pinned)

Every heavy-slot consumer — A4's pre-market ranking, A6's nightly review,
A7's EOD narrative, A5's thematic pass — has been silently falling to
deterministic fallback. The 2026-07-23 emails surfaced it ("deterministic
fallback mode" in the morning briefing was actually the A8 narrative model
*truthfully describing* A4's 07:00 fallback; the EOD "model offline" line
was a real A7 fallback).

Probe evidence (2026-07-24, live against :8084):

- Thinking ON (status quo): the 122B narrates prose inside its implicit
  `<think>` block ("Thinking Process: 1. Analyze the Request…"), the
  `response_format` JSON grammar never bites (it constrains `content`
  only), generation burns the ENTIRE max_tokens budget in reasoning,
  `finish_reason=length`, `content=""`. The model even remarks it cannot
  see the schema. Every consumer then hits `Expecting value: char 0`.
- The unit's `--reasoning-budget 0` flag is demonstrably ignored by this
  build; the `/no_think` soft switch is ignored by this template.
- **`chat_template_kwargs: {"enable_thinking": false}`** (per-request):
  schema-valid JSON in `content`, 52 tokens, `finish_reason=stop`. Cure.

This supersedes the v0.11.9 understanding: reading `reasoning_content` was
correct but insufficient — with real (non-trivial) prompts the model never
finishes its JSON inside the think block at all.

## Changes

1. **`LlamaCppBackend` gains a `disable_thinking` config flag** — when set,
   every request carries `chat_template_kwargs {"enable_thinking": false}`.
   Default false: the triage/analyst slots are untouched (they work today).
   — `src/a1_triage/backends.py`
2. **SlotManager passes the flag from the slot config** — covers
   A4/A5/A6-nightly/A7/A8 in one place. — `src/a7_report/service.py`
3. **`disable_thinking: true` on the heavy block** of `config/a4.yaml`,
   `a5.yaml`, `a6.yaml`, `a7.yaml`, `a8.yaml`.
4. **A13 chat wake fix:** `config/a13.yaml` woke `llama-analyst.service`,
   which does not exist on the Spark (`llama-a2.service` serves :8081 since
   the Qwen3.6-27B upgrade) and isn't in sudoers — every off-hours chat
   wake failed. Now wakes `llama-a2.service` like every other agent.

## Files

REPLACED: `src/a1_triage/backends.py`, `src/a7_report/service.py`,
`config/a4.yaml`, `config/a5.yaml`, `config/a6.yaml`, `config/a7.yaml`,
`config/a8.yaml`, `config/a13.yaml`
NEW: `tests/unit/test_backend_nothink.py`, `patch-notes-v0_12_4.md`,
`v0_12_4-deploy-guide.md`

No migrations. No new services. Heavy-consuming agents are oneshot timers —
they pick the fix up on their next scheduled run; only `a13-chat` needs a
restart (it reads its wake config at startup).

## Tests

3 new unit tests pinning the request shape (flag adds the template kwarg;
default request unchanged — analyst slot untouched; SlotManager passes the
flag per slot). v0.11.9's `_extract_text` tests all still pass (the
fallback read-path stays as belt-and-braces). Suite: **332 passed** (same
single pre-existing env-sensitive failure). Root cause and cure both
verified live on the Spark's own heavy server by operator-run probes.

## What fixed emails look like

Next A5 (21:30 ET tonight), A4+A8 (07:00/07:35 ET), A6 (nightly), A7
(15:35 CT): the briefing/report lead paragraph should be real model prose
about the session — no "deterministic fallback", no "(narrative
unavailable — model offline…)". Heavy generations should also get much
FASTER (52 vs 700 tokens per call in the probe).

## Rollback

`git checkout v0.12.3` (or v0.12.3b) + restart a13-chat. No state to undo.
