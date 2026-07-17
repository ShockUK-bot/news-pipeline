# v0.5.8 — llama unit fix for Qwen3.5/3.6 thinking-channel routing (2026-07-17)

Found live during the 2026-07-17 model-upgrade deploy, on the Part 4 smoke test (which exists
for exactly this). Ops files only; no Python source, config, or schema
changes; tests unaffected.

## Symptom

With the model-upgrade units on llama.cpp b10064, both new models returned an EMPTY
`content` field — the JSON landed in `reasoning_content` instead. The
pipeline reads only `content`, so every agent call would have been an
invalid-output REJECT. `--reasoning-budget 0` (the v0.4.3 fix for Qwen3) is
not sufficient for the Qwen3.5/3.6 template generation: the server's
reasoning parser routed all output to the thinking channel, and the JSON
grammar prevents the model from emitting the `</think>` tag that would close
it. Known upstream issue (ggml-org/llama.cpp #20182).

## Fix

Added to the llama-server flags in all three units, alongside (not replacing)
`--reasoning-budget 0`:

    --chat-template-kwargs '{"enable_thinking":false}'

The chat template then never opens a thinking block, and the server's default
reasoning parsing strips the forced-empty `<think>\n\n</think>` prefix, so
`content` carries clean JSON. Verified live on the Spark: both slots return
`{"ok": true}` in `content`; triage decode ~35 tok/s, analyst ~12.5 tok/s
(faster than the old 32B).

Note: `--reasoning-format none` was tried during diagnosis and REJECTED — it
leaks the literal `<think></think>` text into `content`. Do not add it.

Thinking stays disabled on the heavy slot too for JSON-contract consistency;
revisit if a future off-hours consumer wants long-form reasoning (that would
be a per-request or dedicated-endpoint decision, not a unit default).

## Changed files

- ops/systemd/llama-a1.service
- ops/systemd/llama-a2.service
- ops/systemd/llama-heavy.service

## How to apply on GitHub (browser)

1. On the repo page: Add file -> Upload files.
2. Drag the `ops` folder and this file (`FIXES-v0.5.8.md`) into the upload
   area — the three service files replace the model-upgrade ones.
3. Commit message: "v0.5.8: llama unit fix (enable_thinking false for Qwen3.5/3.6)".
4. Releases -> Draft a new release -> tag `v0.5.8` on main -> Publish.

## How to apply on the Spark

Already applied live in /etc/systemd/system during the deploy (via sed).
To sync the repo checkout so git matches what's running:

    git -C /opt/pipeline fetch --tags
    git -C /opt/pipeline checkout v0.5.8

No service restarts needed — the running units already have the flag.
