# v0.13.9 — Broker refusals become decisions (the silent-short incident)

> Re-cut of the pack briefly circulated as "v0.13.3" on 2026-08-21 — that
> number was already taken by the RSS-hardening release. Same fix, rebased
> and re-tested on v0.13.8. If you downloaded the old `v0_13_3-pack.zip`,
> delete it; this supersedes it.

**Incident (2026-08-17 → 08-21, four trading days):** the live short book
generated 7 real SELL_SHORT intents (MSTR, SNY, NBIS×2, SPCX, UBER×2).
Alpaca refused every one with **HTTP 403** — the account was cash-type
(`multiplier 1`, `no_shorting: true`, per safety rule 24's original
containment design); longs filled 8/8 throughout. C4 translated only
**422** into a clean `BROKER_REJECT`; a 403 raised an unhandled exception,
retried 5×, dead-lettered, and left each intent `PENDING` — **no journal
row, no alert, four days of invisible failure.** Found by operator query;
confirmed in `queue.messages.last_error` and c4-exec journalctl.

**The operator fixed the Alpaca side on 2026-08-21** (margin 2×,
`shorting_enabled: true`, Reg-T BP ~$177k), which unblocks shorts even
without this release. This release fixes the SYSTEM half — the silence:

1. **`common/broker.py`** — order-POST responses of 403 *and* 422 now
   raise `BrokerReject` carrying the broker's own message (pure helper
   `order_reject_message`, unit-tested). C4's existing handler journals
   `BROKER_REJECT` and marks the intent `REJECTED`. A refusal is a
   decision, not a transport error; transport errors (5xx, 429) retry as
   before.
2. **Fail BEFORE the broker.** C4 reconciliation publishes the account's
   capability to `journal.control`: `shorting_enabled` ("1"/"0") and
   `account_multiplier` (plus the existing `regt_buying_power`). A3
   (pre-model, pre-intent) and C4 pre-flight veto short entries with
   **`ACCOUNT_NO_SHORTING`** while the flag is "0". Self-clearing: the
   flag updates at every reconcile.

**Lesson recorded:** every deterministic external refusal must map to a
journaled veto. The DLQ is for conditions that can heal; a mis-configured
account cannot heal by retrying. (Related audit candidate on the
housekeeping list: other non-422 broker responses on non-POST paths.)

**Housekeeping in the guide:** the 7 stuck `PENDING` intents are marked
`REJECTED` (their queue messages are already dead-lettered; new signals
mint new intent ids). The 7 refused signals double as accidental shadow
data — to be priced against the tape in the next review session.

**Release contents:** 5 replaced files (`src/common/broker.py`,
`src/a3_risk/service.py`, `src/c4_exec/reconcile.py`,
`src/c4_exec/service.py`, `tests/unit/test_shorting.py`) + these notes +
the deploy guide. No migration, no config changes. Restart a3-risk +
c4-exec. Tests: **727 passed** in the build sandbox (v0.13.8's 725 + 2 new
reject-mapping tests); on the Spark expect `1 failed, 727 passed` (the
pre-existing `test_triage_v047` failure, `test_cik_map` deselected).
