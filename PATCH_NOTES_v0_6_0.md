# v0.6.0 — Phase 5: A12 Position Guard (verdict-only) (2026-07-18)

Implements Phase 5 of `trading-system-baseline` v0.5: the A12 Position Guard,
the consumer of `signal.guard` (which the router has been producing since
Phase 2 — the queue finally has its agent).

## What A12 does

News reasoning now covers the way OUT of a trade, not just the way in. When
a triaged item touches an open position (including corrections and items A1
scored immaterial), the router enqueues it on `signal.guard` at priority 0.
A12 evaluates it against the position's ORIGINAL entry thesis and the
`news_checkable` invalidation watch-list A2 authored at entry, and emits a
grammar-constrained verdict:

```json
{"thesis_intact": bool, "recommended_action": "hold|tighten_stop|exit",
 "urgency": "high|medium|low", "confidence": 0-1,
 "watch_hits": ["matched watch-list entries"], "reason": "..."}
```

**Verdict-only in v1 (baseline rules 10/12/16):** the verdict is journaled
(stage `GUARD`, agent `A12`) and written to `journal.guard_ledger`
(`auto_executed=FALSE`, `action_taken='JOURNALED'`); it appears on the
dashboard decision tape. There is deliberately NO execution code path — not
a disabled one. Risk-increasing actions are not schema-representable (the
only expressible actions are hold / tighten_stop / exit). Auto-execution of
risk-reducing actions is a later promotion through the A9 channel with its
own enablement criteria.

## Mechanics

- **Staleness gate (code, pre-model):** items older than
  `guard.max_age_minutes` (config, 240) journal `GUARD/EXPIRED` and burn no
  tokens. This is also how the pre-Phase-5 `signal.guard` backlog (~44
  orphaned fan-outs, see a13-chat-agent-design §7a) drains on first start —
  as journaled rows, not a silent purge.
- **Position re-resolution:** routed position_ids are re-checked against
  `journal.positions` at evaluation time; none still OPEN →
  `GUARD/NO_POSITION`.
- **Analyst-slot policy:** A12 shares :8081 with A2/A3-discretion/A13 and
  yields to no one (A13's slot.py already yields to `signal.guard` depth).
  Probe-first wake-on-demand off-hours, identical discipline and sudoers
  rule as deployed A13 (`/etc/sudoers.d/a13-wake` — same user, same command;
  no new sudoers entry).
- **Degradation (baseline §11.2):** analyst server down after a wake
  attempt, or dying mid-call → `GUARD/ALERT_ONLY` decision naming the
  affected positions + `guard` health row DEGRADED. Position-touching news
  is never silently dropped and never wedges the queue.
- **Discipline inherited from A1/A2:** grammar constraint server-side +
  pydantic validation code-side, one retry then `REJECT`; cross-field rule
  enforced in code (`thesis_intact=false` cannot recommend `hold`);
  redelivery no-ops via decision-row dedup per (signal, revision, position);
  infra errors → `queue.fail()` → DLQ; `guard` heartbeat for the dashboard.

## Files

NEW (no existing file is modified):
`src/a12_guard/__init__.py`, `src/a12_guard/schema.py`,
`src/a12_guard/prompt.py`, `src/a12_guard/context.py`,
`src/a12_guard/wake.py`, `src/a12_guard/service.py`,
`config/a12.yaml`, `ops/systemd/a12-guard.service`,
`ops/a12-schema-probe.py`, `tests/unit/test_a12_guard.py`,
`tests/integration/test_guard_flow.py`, `PATCH_NOTES_v0_6_0.md`,
`DEPLOY-v0_6_0.md`.

Plus a one-line pencil edit: `pyproject.toml` version → `0.6.0`.

No schema changes (the Phase-1 journal schema already reserved stage
`GUARD`, `journal.guard_ledger`, and exit layer `GUARD`). No config edits to
existing files. No pipeline.env changes.

## Tests

23 new (12 unit `test_a12_guard.py` + 11 integration `test_guard_flow.py`):
verdict contract incl. risk-increase inexpressibility and grammar-safety
pins; staleness/backlog drain with zero model calls; NO_POSITION;
watch-list-hit exit; invalid-output REJECT; cross-field retry; redelivery
no-op; multi-position fan-out; ALERT_ONLY degradation; wake probe-first
logic. Full suite green on PG16 in the build environment (247 = 224-file
v0.4.7 tree + these 23; expect the Spark's v0.5.9 count + 23).

## Rollback

`sudo systemctl disable --now a12-guard` + `git checkout v0.5.9`. The queue
just resumes accumulating (A13's depth check ignores messages older than
15 min, so chat is unaffected).
