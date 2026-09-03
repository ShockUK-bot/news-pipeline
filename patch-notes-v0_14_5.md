# v0.14.5 — The analyst was thinking out loud, and the scanner was blind to big liquid movers (2026-09-03)

**Base:** v0.14.4 (the tree the Spark is running). Two fixes from one
investigation: the analyst thinking-leak that stopped all trading, and the
scanner's mega-liquid-name blind spot that was the original MSTR question.
Config + code + tests. No migration, no new dependency, no model change.

## What triggered it

Operator asked why MSTR was not traded on 2026-09-03 (it ran +13.5%). The
scanner miss on MSTR was real and separate (REL_VOLUME 2.94× vs the 3.0×
bar — the mega-liquid-name blind spot, tracked for its own release). But
the audit uncovered something far larger sitting underneath it:

**The analyst produced invalid output on 128 of ~196 calls (~65%) that
day, and the system traded nothing.** Every failure was
`output is not valid JSON: Expecting value: line 1 column 1 (char 0)` or
an "Unterminated string", and every raw output began the same way:

```
Let me analyze this scanner signal for SNOW carefully.
Key facts:
- SNOW is up +19.78% ...
```

The model was **narrating its reasoning as prose instead of emitting
JSON.** SNOW (+19.8% on 19.8× relative volume — the best-scored scanner
setup of the day) reached the analyst in 68 seconds (the v0.13.8 fast lane
working perfectly) and then died here, not on a judgment call. So did
HOOD, HPE, CRCL, BIAF, and ~120 news signals.

## Root cause — a thinking model with thinking left on

`config/a2.yaml` runs `qwen3.6-27b-q5_k_m`, a **hybrid reasoning model with
thinking ON by default.** The analyst slot moved to it from the old
non-thinking `qwen3-32b` (the ROLLBACK note in a2.yaml records the swap)
— and **`disable_thinking: true` was never added.**

`src/a1_triage/backends.py` has documented this exact failure since
**v0.12.4** (its own code comment): with thinking on, the model narrates
inside its `<think>` block; the `response_format` JSON grammar constrains
`content` only, never the thinking channel; generation burns the whole
`max_tokens` budget reasoning and returns empty or truncated `content`.
The fix proven then was the per-request chat-template kwarg
`{"enable_thinking": false}`, gated by a config flag. **A4, A5, A6, A7 and
A8 have set `disable_thinking: true` since v0.12.4. The analyst slot was
missed** — because when that fix shipped, A2 was still on the non-thinking
32B and did not need it. The later model upgrade re-opened the hole.

This is why nothing traded: a signal that cannot become a valid thesis
never reaches the gate, the risk sizer, or the broker.

## The fix

**1. `disable_thinking: true` on every live-trade-path qwen3.6-27b slot:**

- `config/a2.yaml` — the analyst (the proven break).
- `config/a12.yaml` — the position guard (its "is the thesis broken NOW"
  verdict is JSON; the same leak fails it closed).
- `config/risk.yaml` — A3 bounded-discretion sizing (leaks prose → every
  trade silently falls back to profile defaults, the v0.12.22 shape).

All three share the `:8081` slot and the identical latent defect;
`enable_thinking` is a per-request kwarg, so each service controls it
independently. A13 (chat) stays on `qwen3-32b` and is unaffected.

**2. A silent invalid-output storm now ALARMS (`src/a2_analyst/service.py`).**
The deeper lesson is the one this project keeps re-learning (a1-zombie,
silent-short): a 65%-failure day passed with no signal because the analyst
heartbeat only ever said `OK`. New pure `analyst_health()` tracks a rolling
window (20) of model-output outcomes; once there are ≥10 samples and ≥50%
are invalid, the periodic heartbeat writes **DEGRADED** with the reason
"model may be emitting prose not JSON (check disable_thinking)". C7 watches
`analyst` (alert_min 5) and the v0.14.4 morning briefing surfaces every
non-OK row in its banner — so this specific failure can never again run a
full session unseen. Knobs in `a2.yaml` (`health_window`,
`health_min_sample`, `health_max_invalid_frac`); defaults are safe.

## Why this is low-risk

`disable_thinking: true` is not a new mechanism — it is the same one five
other agents have run in production for two months. For a schema-
constrained JSON producer, thinking only burns tokens and leaks prose;
turning it off is the intended mode, not a tuning trade-off. The health
guard is pure, additive, and alert-only — it changes no trading behaviour.

## Verify after deploy (the one thing to confirm)

The fix relies on the `qwen3.6-27b` GGUF honouring the `enable_thinking`
kwarg (it did for the 122B on this llama.cpp build; the guide's Part 7
confirms it on the analyst slot with a live invalid-output-rate query). If
the rate does NOT collapse next session, the kwarg is being ignored by
this template and we pivot to a prompt-level `/no_think` or a parser-side
preamble strip — a fast follow-up. The health guard makes that visible
either way.

## Second fix — the scanner can finally see big liquid movers (the original MSTR question)

This is the mega-liquid-name blind spot, folded in at the operator's
request. It is what actually made MSTR invisible on 2026-09-03: its only
scanner row all day was `REL_VOLUME` at **2.94×** against the flat 3.0×
bar, even as it ran +13.5%. The flat bar is calibrated for low-float
spikers (real moves print 10–50×); a mega-liquid name's 20-day baseline is
already so large that 3× of it — a vast dollar amount and a rare event — is
rejected. Third instance of this exact miss (WDAY review, 08-19 MSTR, now).

Two coupled changes in `src/c10_scanner/rules.py`, both fully config-driven
in `config/scanner.yaml`:

1. **Liquidity-tiered rel-volume bar** (`rel_volume_bar`). A name with
   ADV20 ≥ `large_cap_adv_dollars` ($1B) needs only `large_cap_min_rel_volume`
   (2.0×) instead of 3.0×. A microcap still needs the full 3.0×. The relaxed
   bar can only ever be *lower* than the base. `large_cap_adv_dollars: 0`
   disables the tier.
2. **A liquidity term in the ranking score** (`score_candidate` +
   `liquidity_term`). The filter fix alone wasn't enough — at 2.94× MSTR's
   *old* score was ~0.50, under the 0.60 emit floor, because the score
   weighted rel-volume 0.45 on a linear ÷10 scale (a mega-cap's multiple is
   structurally small). The single rel-volume weight is now split into
   rel-volume (abnormal *multiple*) 0.30 + liquidity (absolute dollar
   *size*, log-scaled $25M→$2.5B) 0.15. MSTR at 09:51 now scores **0.6075**,
   just over the floor — it would emit early, at +7.4%, with room to run.

Guardrails, verified in tests: a microcap at 2.94× is still rejected; a
huge-but-barely-moving name still scores under the floor (size alone can't
carry it); ranking still orders by conviction; and setting `score_w_rel:
0.45` + `score_w_liquidity: 0.0` + `large_cap_adv_dollars: 0` restores the
exact pre-v0.14.5 behaviour. Every number is an A9-tunable placeholder
pinned to MSTR's real 09-03 figures, same discipline as the gate thresholds.

**Ordering note:** this fix only bites once the analyst emits JSON again
(fix #1) — a perfectly-emitted MSTR still died at the analyst wall on
09-03. Both ship together so the next big liquid mover is both *seen* and
*actable*.

## Not in this release (deliberately)

- **`test_a7_c5.py::test_render_busy_day_with_narrative`** fails on a clean
  v0.14.4 checkout (not caused by this release) — an A7 rendering
  regression from somewhere in v0.14.x, flagged for its own look.

## Files

**REPLACED (7):** `config/a2.yaml`, `config/a12.yaml`, `config/risk.yaml`,
`config/scanner.yaml`, `src/a2_analyst/service.py`,
`src/c10_scanner/rules.py`, `src/c10_scanner/service.py`
**NEW (3):** `tests/unit/test_v0_14_5.py`, `patch-notes-v0_14_5.md`,
`v0_14_5-deploy-guide.md`

No migration. No new dependency. No model change (a config flag on the
existing model).

## Tests

16 new (`tests/unit/test_v0_14_5.py`) — 9 for the analyst fix + guard, 7 for
the large-cap volume handling — all passing here. Full unit suite:
**2 failed, 767 passed** — both failures pre-exist on clean v0.14.4
(`test_triage_v047::test_confidence_required`, long-standing; and
`test_a7_c5::test_render_busy_day_with_narrative`, the v0.14.x A7
regression noted above). The 16 add to v0.14.4's 751 passing.

## Rollback

`git checkout v0.14.4`, restart `a2-analyst a12-guard a3-risk c10-scanner`.
Which restores the thinking-leak and the scanner blind spot. There is no
reason to. (To keep the analyst fix but revert only the scanner scoring,
the config knobs above do it without a rollback.)
