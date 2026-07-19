# v0.9.0 — Phase 8: A5 thesis store + A6 position review (2026-07-19)

Implements Phase 8 of `trading-system-baseline` v0.5: the consumer of
`signal.thesis` (accumulating unread since Phase 2, fed by router rule 3 and
A4's thesis lane since Phase 7) and the position-review layer (baseline L5
model layer nightly + L6 overnight-hold check). Both new agents are
**recommendation-only** — no order is placed, no stop is moved; auto-apply
stays off exactly like A12 v1 (enabling it is a baseline rule-12 evidence
decision for a later phase).

## A5 Macro/Thematic — nightly 21:30 ET, Sunday = deep pass

The system's long memory. Every night it drains the thesis lane into a
persistent store: `journal.theses` (driver, direction, beneficiaries,
invalidation conditions, confidence) + `journal.thesis_evidence` (dated,
per-item, polarity-tagged — redelivery no-ops via a UNIQUE constraint).

1. **Bulk expiry (code, one SQL pass):** lane messages older than 168h
   retire as ONE `THEMATIC/EXPIRED_BULK` row — the multi-week backlog
   drains on first start with zero tokens.
2. **Staleness expiry (code rule, before the model runs):** ACTIVE theses
   with no evidence in `store.stale_weeks` (6) → `EXPIRED` +
   `THESIS_EXPIRED` row. The model cannot veto or hide this.
3. **Slot first, claims second:** heavy via the shared SlotManager (A7's,
   analyst fallback, ownership rule). No slot → `SKIPPED_NO_MODEL`, the
   lane stays queued for tomorrow — deliberately NO deterministic fallback:
   an unread thesis item tonight beats a blind guess.
4. **ONE grammar-constrained call** over (active theses + top-K fresh
   items; 25 nightly / 60 on the Sunday deep pass) → per-item ops
   (evidence with polarity — contradicting evidence is explicitly wanted —
   or ignore), rare fully-specified new theses, and per-thesis reviews
   (confidence moves, invalidate, realized). Code validates every
   referenced id: unknown thesis → downgraded to ignore and counted;
   thesis ids are code-minted (`th-<year>-<seq>`), never model-invented.
5. **Digest:** `THEMATIC/DIGEST` anchor (idempotency per ET date) + a
   digest email via `journal.outbox` kind `ALERT`. Invalid model output →
   claims released back to the queue (`REJECT` row, no digest — a manual
   same-night rerun may retry).

**Router stub activated:** `router.facts.thesis_matches` now reads the
`journal.thesis_watchlist` view (one row per ACTIVE-thesis beneficiary
ticker) — the `routing.thesis_matches` field journaled on every triage
decision is finally live, feeding A2's context and A4. Defensive: any store
error degrades to `[]` so the intraday path never depends on Phase-8 tables.

## A6 Position Review — 15:45 ET EOD check + 20:00 ET nightly deep review

- **EOD overnight-hold check (weekdays 15:45 ET, SHORT lane):** the
  baseline L6 explicit EOD decision — remaining expected move vs. gap
  exposure, ONE call on the RESIDENT analyst slot (the heavy model never
  runs during market hours — memory rule). Verdicts journal as
  `HOLD_OVERNIGHT` / `EXIT_EOD_RECO` + an `OVERNIGHT_HOLD_DECISION`
  position event (reserved in the Phase-1 schema for exactly this).
  Model omissions default to hold (flagged); model down →
  `SKIPPED_NO_MODEL` and C4's code overnight rule governs unaided.
- **Nightly deep review (weekdays 20:00 ET, whole book, heavy slot):** per
  position — is the ORIGINAL thesis intact, is the evidence clock running
  (news recency on the name since entry = the long lane's time stop), and
  were today's A12 guard actions appropriate? Verdicts journal as
  `HOLD` / `TRIM_RECO` / `EXIT_RECO` with the full fact pack in payload +
  a `POSITION_REVIEW` position event. **The code-side staleness rule runs
  first** and journals `STALE_FLAG` rows even with no model — a dead model
  cannot hide a stale position. Trim/exit/stale recommendations render into
  ONE `ALERT` email (quiet nights send nothing).

Heavy-slot choreography stays sequential and off-hours: A7 16:35 ET → A6
20:00 ET → A5 21:30 ET, each start/stopping the heavy slot under the
v0.7.0 ownership rule (probe-first; an operator-started session is never
killed).

## Journal & idempotency

New stage `THEMATIC` (A5; CHECK widened by migration 004). A6 uses the
reserved `POSITION_REVIEW` stage. Anchors: `DIGEST` (A5), `EOD_SHEET` /
`REVIEW` (A6) — one per ET date, rerun no-op. A5 evidence dedup via
`UNIQUE (thesis_id, item_id, item_revision)`; A6 skip rows
(`SKIPPED_NO_SESSION` / `SKIPPED_NO_POSITIONS` / `SKIPPED_NO_MODEL`) cost
zero tokens.

## Files

NEW (27): `schema/migrations/004-thesis-store.sql` (journal schema v4:
`theses`, `thesis_evidence`, `thesis_seq`, `thesis_watchlist` view;
stage CHECK + position_events CHECK widened — additive only),
`src/a5_thematic/{__init__,schema,prompt,render,store,service}.py`,
`src/a6_position_review/{__init__,schema,prompt,render,context,service}.py`,
`config/{a5,a6}.yaml`,
`ops/systemd/{a5-thematic,a6-eod,a6-nightly}.{service,timer}`,
`tests/unit/{test_a5_thematic,test_a6_review}.py`,
`tests/integration/{test_thesis_flow,test_position_review_flow}.py`,
`PATCH_NOTES_v0_9_0.md`, `DEPLOY-v0_9_0.md`.

MODIFIED (1): `src/router/facts.py` — the Phase-8 `thesis_matches` stub
becomes live (documented in the module docstring; everything else in the
file is unchanged). Plus the pencil edit: `pyproject.toml` → `0.9.0`.

No new sudoers (the a7-heavy rule covers A5/A6-nightly — same user, same
commands; a13-wake covers the analyst wake). No env changes. One additive
schema migration.

## Tests

30 new (19 unit + 11 integration). Unit: thematic + review contracts with
grammar-safety pins, op resolution (unknown-thesis downgrade, anchor
precedence), thesis-id minting, staleness cutoff/classification (a young
position can never be stale), R-progress, prompts, both renderers.
Integration (real PG16): new-thesis creation with code-minted id + live
watchlist + `thesis_matches` activation; evidence attach with clock update
and redelivery no-op; model invalidate + code staleness expiry; no-slot
skip leaves the lane unclaimed; invalid output releases claims with no
digest; nightly verdicts + STALE_FLAG + alert email + rerun no-op; EOD
SHORT-lane-only + omission-defaults-to-hold + rerun no-op; no-model
degradation still reports code-rule flags; empty-book skip.

Build environment (PG16, fresh DB, migrations 001→004): **348 passed**
(v0.8.0's 318 + 30).

## Rollback

`sudo systemctl disable --now a5-thematic.timer a6-eod.timer
a6-nightly.timer` + `git checkout v0.8.0`. Migration 004 is additive —
leave it in place; the thesis lane resumes accumulating exactly as before
Phase 8 (`thesis_matches` returns to `[]` with v0.8.0's stub).

---

## System status after this deploy (2026-07-19)

Phases 1–8 live: C1/C2 → A1 triage/router (thesis-match facts now live) →
A2/C3/C8 → A3/C4 (paper) + A12 guard → A4 pre-market + open handoff →
A5 thesis store → A6 position review (EOD + nightly, recommendation-only) →
A7/C5 daily email + A13 chat + C6 dashboard.

**Remaining build order:** Phase 9 (A8 morning briefing — A4 sheet + A5
watchlist + A6 recommendations + calendar, the outbox/mailer are ready),
Phase 11 (A11 eval rollups, A9 weekend review, A10 upgrade scan, C9
replay). **Open config work:** §14 gate-threshold tuning (first week of SIP
veto data lands this week — planned for the weekend of 07-25), earnings
calendar source (removes EARNINGS_UNKNOWN), sector source (activates the
sector-heat clip), A12 auto-execution criteria (guard-ledger evidence), A6
auto-apply criteria (this release's ledger starts accumulating the
evidence). Model-watch window closes ~07-24.
