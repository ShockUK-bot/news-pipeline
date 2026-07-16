# A13 Operator Chat — Design (v1.0)

Companion to `trading-system-baseline-v0_5` (extends the agent inventory) and
`c6-dashboard-spec-v1_3` (the CHAT tab that fronts it). Ships as patch
`a13-chat` against pipeline v0.4.7.

---

## 1. Purpose & posture

A13 is a conversational lens over the journal plus an advisory analyst for
prospective tickers, driven from a new CHAT tab on the C6 dashboard. It
answers three classes of operator question:

1. **What did the system do?** — trades placed, trades closed, P&L, decision
   traces ("what happened to signal X?").
2. **Why was a trade vetoed?** — VETO decisions with their machine reason
   codes (`GATE_NO_CONFIRM`, `HEAT_CAP`, `SIZE_CLIPPED`, `KILL_SWITCH`, …)
   translated to plain language, codes quoted.
3. **What about ticker X?** — an advisory review against recent news, live
   market context, regime, and the ticker's own journal history, ending in a
   stance (`consider_long | watch | avoid | no_view`).

Posture: **A13 is advisory and read-mostly.** It cannot trade, size, move
stops, or touch control flags. Its single pipeline-affecting act — *filing a
ticker for evaluation* — is operator-proposed by the model, operator-confirmed
on the dashboard (kill-token gated), and finally disposed by code gates
(§5). A filed ticker enters the **existing synthetic lane** and runs the full
A1 → A2 → C3 → A3 gauntlet with no shortcuts; A13 never enqueues to
`signal.analyst`, `signal.gate`, or `exec.intent` directly.

## 2. Model slot policy — lowest-priority tenant of the Analyst slot

A13 uses the Analyst llama-server (:8081, Qwen3-32B) — the decided option
over a dedicated model (no spare VRAM budget on the Spark) and over the Fast
slot (8B is too weak for "should I consider X" reasoning).

Priority discipline (`src/a13_chat/slot.py`): before **every** model call,
A13 reads the READY depth of `signal.analyst` + `signal.guard` in
`queue.messages` and sleeps (`poll_secs: 2`) until they drain. A2 escalations
and A12 guard checks always go first — protecting capital and evaluating live
news outrank chat. Because a human is waiting, the yield is bounded
(`max_wait_secs: 90`); past the bound A13 proceeds and the reply carries a
"slot contended" caveat.

Accepted residual risk: a chat generation already in flight when pipeline
work arrives finishes first. `max_tokens: 900` keeps that window to seconds
at Spark token rates (~10–15 tok/s ⇒ worst case ~60–90 s; tune down if A2
latency percentiles move — A11 tracks `latency_ms` per decision).

## 3. Two-call flow — models propose, code disposes

**Call 1, planner** (grammar-constrained `PlannerOutput`): picks 1–5
retrieval packs from a fixed enum with typed parameters. The model never
writes SQL. Invalid after retries → deterministic fallback plan
(open positions + closed trades 7d + vetoes 7d + control state).

**Retrieval** (`retrieval.py`): each pack is parameterized SQL written in
code — `open_positions`, `closed_trades`, `position_detail`, `vetoes`,
`decision_trace`, `ticker_news`, `ticker_snapshot` (live quote, ATR14, ADV20,
regime, open-position check via `common.marketdata`), `performance`,
`control_state`. Failing packs degrade to `{"error": …}` entries; the fact
sheet is truncated to a char budget with truncation recorded in-band.

**Call 2, answer** (grammar-constrained `AnswerOutput`): prose answer
computed **from the fact sheet only** (rule 5: numbers by code, narrative by
the model), optional `recommendation {stance, rationale}`, optional
`filing_proposal {ticker, anchor_item_id, rationale}`, caveats. The prompt
hard-requires that every number quoted appears in the fact sheet and that
missing/errored packs are said out loud, never estimated.

## 4. Data model & transport

Migration `002-chat.sql` (journal schema v2, additive):

- `journal.chat_sessions`, `journal.chat_messages` (role OPERATOR/ASSISTANT,
  kind ASK/ANSWER/FILE_REQUEST/FILE_RESULT/ERROR, `fact_sheet` JSONB,
  `proposal` JSONB, `decision_id` lineage, PENDING/DONE/ERROR status).
- `decisions.stage` CHECK widened with `'CHAT'` (NOT VALID + VALIDATE to keep
  the exclusive lock brief).
- View `journal.dash_chat` for the dashboard.

Transport: dashboard inserts the OPERATOR row and enqueues **`chat.request`**
(new queue, priority 200 — below everything) in one transaction; A13 consumes,
answers, and inserts the ASSISTANT row + flips the request row's status in one
transaction. At-least-once safety: a redelivered request that already has a
`reply_to` row is acked as a no-op. Queue contract `chat.request/1`:
`{body: {message_id, session_id, kind: ASK|FILE}}` — the question text lives
in the journal row, not the queue payload.

## 5. Filing for evaluation (the one write)

Chain of custody: **model proposes → operator confirms → code disposes.**

1. `AnswerOutput.filing_proposal` may be set only when the stance is
   `consider_long` AND `ticker_news` returned a fresh item to anchor on.
2. The dashboard renders the proposal with a FILE FOR EVALUATION button;
   confirming requires the kill token (`DASH_KILL_TOKEN`) — the third
   token-gated write action after kill switch and capital. Writes an audit
   row (`CHAT_FILE_REQUESTED`) + a FILE_REQUEST chat row + enqueue.
3. `filing.py` code gates, in order: `FILING_DISABLED`, `KILL_SWITCH`,
   `BREAKER`, `NO_ANCHOR` (anchor item must exist in the news store),
   `STALE_ANCHOR` (anchor `received_ts` within `anchor_max_age_hours: 72` —
   the system trades news, not hunches; with no recent news there is nothing
   for the pipeline to evaluate and A13 says so instead of filing).
4. On pass, ONE TRANSACTION: `decisions` row (stage `CHAT`, agent `A13`,
   action `FILED`, signal_id `chat-<message_id>`) + audit row
   (`CHAT_SIGNAL_FILED`, operator attributed) + `signal.synthetic` enqueue
   (`synthetic_id = op-<decision_id>-<ticker>`, relation
   `operator_inquiry`, `derived_from_decision` = the CHAT decision).
5. A1 re-triages the anchor item FOR that ticker (existing §10 synthetic
   handler, zero new code in A1), then A2/C3/A3 apply every normal gate.
   Thesis lineage is intact, so `NO_THESIS_LINEAGE` holds; idempotency holds
   (duplicate filing → same dedup key → no-op; `intent_id` chain unchanged).

Double-filing the same proposal message is rejected dashboard-side (409).

## 6. Failure modes

- Planner invalid → fallback plan (answer still produced, caveated).
- Answer invalid after retries → ERROR chat row, request marked ERROR, queue
  message acked (journaled failure, not an infinite retry — A1/A2 REJECT
  discipline).
- Infrastructure errors → `queue.fail()` backoff → DLQ to quarantine.
- Market data down → `ticker_snapshot` degrades to an error entry; the answer
  must state it.
- Model server down → chat requests back up on `chat.request`; pipeline
  unaffected (separate queue, C6 shows `chat` component health;
  `set_health('chat', …)` heartbeats).
- A13 down → dashboard chat shows PENDING rows only; zero trading impact.

## 7. Config, deploy, ops

- `config/a13.yaml` (model / slot / retrieval / filing / answer sections).
- `ops/systemd/a13-chat.service` (same shape as a2-analyst).
- Health component key: `chat`.
- Tests: `tests/unit/test_a13_chat.py` (15 tests, DB-free: contracts,
  freshness gate, truncation, prompts, fallback plan). Integration tests
  against `trading_test` should cover: ASK round-trip with stub backend;
  FILE happy path lands a `signal.synthetic` message + CHAT/FILED decision +
  audit row; each filing rejection code; duplicate-delivery no-op.

**Deployment timing:** follow the standing loop — **after 16:00 ET**, test
against `trading_test`, commit, restart touched services, config_version
check. The dashboard restart itself is safe intraday (read-only console; kill
enforcement lives in C4 via the DB flag), but this patch is NOT dashboard-only:
migration 002 takes a brief ACCESS EXCLUSIVE lock on `journal.decisions`
(every stage writes it during RTH), and first-day chat traffic on the shared
Analyst slot during market hours is unmeasured. Deploy order: (1) migration →
(2) `a13-chat.service` → (3) dashboard (router + tab) → (4) smoke: ask "what's
open?", file a test ticker under `trading_test`. Rollback: stop a13-chat,
remove the dashboard tab include — the chat tables are inert without them.

## 8. Baseline deltas (fold into baseline v0.6)

- Agent inventory: **A13 Operator Chat** — Analyst slot (lowest priority,
  yield protocol) — on-demand, market hours + off-hours — journal Q&A +
  advisory ticker review + operator filing (via synthetic lane).
- §12 rules: chat answers derive numbers exclusively from code-computed fact
  sheets; filing requires operator token + fresh news anchor; A13 never
  writes positions/orders/control.
- C6: third token-gated write action (file-for-evaluation).
- Open item: measure A2/A12 latency impact of chat during the paper soak;
  revisit `max_tokens`/`max_wait_secs`, or move chat to an off-hours-only
  Heavy-slot mode if contention shows up in A11 rollups.
