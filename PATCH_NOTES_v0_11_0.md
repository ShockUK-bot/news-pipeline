# v0.11.0 — Phase 9: A8 Morning Briefing (2026-07-19)

Implements Phase 9 of `trading-system-baseline` v0.5 — the last email in
the agent inventory. One consolidated pre-open briefing (07:35 ET
weekdays) replaces A4's bare sheet email: everything the operator needs
before the open, in one message, every claim traceable to journal rows.

## What the 07:35 ET email contains (facts by SQL, narrative by model)

- **Narrative lead (model):** 2-4 sentences + up to 5 watch items —
  decisions first (exit/trim recos, blackout windows, degradations), then
  the day's character. Grammar-constrained; invalid after retry → the
  briefing ships with "(narrative unavailable)". Never blocked by an LLM.
- **Pre-market sheet (from A4's SHEET decision):** ranked open candidates
  with headlines re-fetched from the news store (citation rule), routing
  counts, the 09:45 entry time.
- **Open positions:** qty/entry/last, R-progress, current stop, and the
  earnings clock per name — `EARNINGS in N sessions` flagged when a held
  name reports within `blackout_warn_sessions` (2). Last night's A6
  recommendation attached to each position it concerns.
- **Standing theses (Phase 8 store):** active theses with beneficiaries +
  last A5 pass stats.
- **Earnings landscape:** market-wide reporter count today + held names
  reporting within 5 sessions.
- **Ops:** queue depths, non-OK health components, newest-item age.

Every section degrades independently and VISIBLY ("not available yet this
morning", "calendar unavailable") — the operator can tell quiet from
broken, and the email always ships.

## One morning email, not two

`config/a4.yaml` now ships `report.email: false` and A4's outbox write is
config-gated: A4 still journals its SHEET decision at ~07:10 (A8's source
and its idempotency anchor) but no longer sends its own email. A8 embeds
the sheet in the consolidated briefing at 07:35. Set `report.email: true`
to restore the separate sheet email at any time.

## Slot discipline at 07:35 (pre-open)

Heavy `autostart: false` and `stop_after_use: false` for A8 — pre-open is
inside the memory rule's caution window and A4 already stopped the slot it
started at 07:00. Probe-first still uses a heavy server the operator left
running; otherwise the resident/woken ANALYST slot narrates (a13-wake
rule). Baseline's "A8: Heavy (narrative only)" is honored opportunistically,
never by booting 77 GB pre-open for a paragraph of prose.

## Journal & idempotency

Stage `SYSTEM`, agent `A8`. Anchor: `BRIEFING` per session date (rerun
no-op); `SKIPPED_NO_SESSION` on holidays (`report.send_on_nonsession`
overrides). The BRIEFING decision payload carries the full fact sheet +
narrative (rule 5 made auditable, same as A7). Outbox kind
`MORNING_BRIEFING` — C5 mails it minutes later, unchanged.

## Files

NEW (12): `src/a8_briefing/{__init__,facts,narrative,render,service}.py`,
`config/a8.yaml`, `ops/systemd/a8-briefing.{service,timer}`,
`tests/unit/test_a8_briefing.py`,
`tests/integration/test_briefing_flow.py`, `PATCH_NOTES_v0_11_0.md`,
`DEPLOY-v0_11_0.md`.

MODIFIED (3): `src/a4_premarket/service.py` (outbox write gated on
`report.email`, default false; SHEET decision unchanged), `config/a4.yaml`
(ships `report.email: false`), `tests/integration/test_premarket_flow.py`
(opts into `email: true` to keep asserting the standalone-email path).
Plus the pencil edit: `pyproject.toml` → `0.11.0`.

No schema changes. No new sudoers. No env changes.

## Tests

7 new (4 unit + 3 integration). Unit: narrative contract + grammar-safety
pin, subject construction (candidates/recos/blackout counts), renderer
determinism on full facts, visible-degradation rendering (missing A4,
missing narrative, DEGRADED health). Integration (real PG16): full
briefing over a seeded journal (A4 sheet embedded with re-fetched
headline, A6 reco attached to its position, earnings clock inside the
blackout window, thesis store, ops section) + rerun no-op; degraded
morning (no A4 row, invalid narrative twice) still ships; A4 email
consolidation (default: SHEET decision without outbox row; `email: true`
restores it).

Build environment (PG16, fresh DB, migrations 001→005): **364 passed**
(v0.10.0's 357 + 7).

## Rollback

`sudo systemctl disable --now a8-briefing.timer` + `git checkout v0.10.0`
(which restores A4's own email — its config there has no gate and the old
code always writes the outbox row).

---

## System status after this deploy (2026-07-19)

Phases 1–9 + earnings source live. The agent-email inventory is complete:
A8 morning briefing (07:35 ET) → A7 EOD report (16:35 ET) → A6 alert
(20:00 ET, only when action is recommended) → A5 digest (21:30 ET).

**Remaining build order:** Phase 11 only (A11 journal/eval rollups →
A9 weekend review → A10 upgrade scan → C9 replay harness). **Open config
work:** §14 gate-threshold tuning (this week's SIP veto data — weekend of
07-25, alongside the first A9-style manual review), sector source (last
null P1 key with short_interest), A12 auto-execution and A6 auto-apply
criteria (ledger evidence accumulating). Model-watch window closes ~07-24.
