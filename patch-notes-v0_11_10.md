# v0.11.10 — Gate defers fast signals until minute bars can exist (2026-07-21)

**Fixes the mid-session `Marketdata: no volume bars for …` dashboard errors
(GM, EMBJ) and, more importantly, stops the gate terminally vetoing exactly
the fast in-session signals a news-momentum system wants.** One new queue
helper, one service change, one config knob. No schema change, no systemd
change. One service restart (`c3-gate`).

## What was actually wrong

The dashboard showed `Marketdata: no volume bars for EMBJ` and the same for
**GM** — during regular hours, on one of the most liquid stocks in the market.
The journal showed the pattern exactly: every `MARKETDATA_MISSING` veto fired
**~57–64 seconds after the item's publish time**:

- GM `alpaca:60578634` — published 13:59:32Z, vetoed 14:00:36Z (64s)
- GM `alpaca:60580397` — published 14:23:29Z, vetoed 14:24:31Z (62s)
- GM `alpaca:60582075` — published 14:52:27Z, vetoed 14:53:24Z (57s)
- EMBJ `alpaca:60582892` — published 15:10:16Z, vetoed 15:11:17Z (61s)

That ~60s is simply the A1 → A2 → C3 pipeline latency on Alpaca's fast news
feed. The bug: minute bars are stamped on minute boundaries and **only exist
once their minute completes**. For news published at 10:00:12, the first bar
the since-news window `[published_ts, now]` can contain is stamped 10:01 and
completes at 10:02. Evaluated at 10:01:12, the window holds **zero completed
bars — for any ticker, GM included**. `avg_minute_volume()` returned `None`,
`vol_mult` was `None`, and the v0.5.9 fail-safe (correctly) vetoed — but as a
**terminal** veto, killing the signal and painting `marketdata: DEGRADED` on
the dashboard.

Replaying the exact same windows a few hours later returned 87, 63, 34 and 14
bars. The data was never missing. The gate was asking a question one minute
before an answer could exist. (This is the same failure family as the July
16–17 no-trade review, which was blamed entirely on the thin IEX feed. The
SIP upgrade fixed the feed; it could not fix the window geometry.)

Net effect before this fix: **every in-session signal that reached C3 within
~2 minutes of publish was auto-vetoed** — the fastest, best signals — while
slower items (wider windows) sailed through. Safety was never at risk
(missing data always vetoes); opportunity was.

## The fix — defer, don't veto

C3 now computes the earliest instant the since-window can physically contain
`min_confirm_bars` (default **3**) completed minute bars. If an in-session
signal arrives before that instant, the gate **defers** it instead of
evaluating: the message goes back on `signal.gate` with a delay
(queue-native `available_ts` delayed delivery — the same mechanism A4 already
uses for open-handoff scheduling) and re-arrives once bars exist, typically
~3–4 minutes after publish. Nothing about the rules changes; the same
message is evaluated once, later, with real data.

Details that matter:

1. **`queue.defer(msg_id, delay_secs)`** (new, `src/common/queue.py`) —
   releases a claimed message with a future `available_ts` and **refunds the
   claim attempt**, so deferral can never push a healthy message toward
   `max_attempts` / the DLQ. Deferral is scheduling, not failure. No SQL
   function or schema change — plain UPDATE on `queue.messages`.
2. **Maturity arithmetic** (`bars_mature_ts` / `defer_delay`,
   `src/c3_gate/service.py`) — pure functions; delay floored at 5s (no
   busy-reclaim spin) and capped at 300s (a future-skewed feed timestamp
   can't park a message for hours).
3. **Only in-session news defers.** Off-session news takes the open-handoff
   rule (gap-based, no `vol_mult`) and is never delayed.
4. **The fail-safe is retained.** `MARKETDATA_MISSING` still vetoes — but now
   it only fires when a **mature** window is genuinely empty, which means a
   trading halt or a real data outage. When that dashboard alert appears
   from now on, it means something.
5. Defers are logged (`gate DEFER`, with ticker and mature time) but write
   **no journal decision** — scheduling isn't an outcome, and one signal
   still produces exactly one GATE decision.
6. `intraday_window_min` (30 min) is untouched: a deferred signal re-arrives
   ~4 minutes after publish, well inside its confirmation window.

Trade-off, stated plainly: the earliest possible entry moves from ~1 minute
after publish (where it was being 100% auto-vetoed, so nothing is actually
lost) to ~3–4 minutes. A 2.5× volume confirmation on a single partial minute
was never a meaningful test anyway; this is the gate finally measuring what
it was designed to measure.

## Files in this pack

- **REPLACED:** `src/common/queue.py` (adds `defer()`),
  `src/c3_gate/service.py` (maturity check + `DeferEvaluation` + consume-loop
  wiring), `src/c3_gate/rules.py` (docstring only — behaviour unchanged),
  `config/gate.yaml` (adds `min_confirm_bars: 3`),
  `tests/integration/test_analyst_gate_flow.py` (two new tests).
- **NEW:** `tests/unit/test_gate_defer.py` (7 tests, incl. the exact GM
  incident shape: published 13:59:32, evaluated 14:00:36 → defer 145s),
  `patch-notes-v0_11_10.md`, `v0_11_10-deploy-guide.md`.

## Validation

Full suite on PostgreSQL 16: **262 unit passed** (7 new) and **119
integration passed** (2 new), including the complete
`test_analyst_gate_flow.py` (11/11): defer round trip (raise → requeue →
invisible until mature → re-claim → PASS, attempt refunded, no journal row)
and mature-empty-window still vetoes `MARKETDATA_MISSING`. Two pre-existing,
unrelated failures are unchanged before/after this patch and fail identically
on an untouched v0.11.9 tree in the build environment:
`test_triage_v047.py::test_confidence_required` (pydantic version wording,
already noted in v0.11.9) and
`test_premarket_flow.py::test_01_full_premarket_run` (wall-clock-sensitive
assertion that only passes outside market hours; the build ran mid-session).

## Rollback

`git checkout v0.11.9` on the Spark, then `sudo systemctl restart c3-gate`.
No schema changes to undo.

## Optional follow-up (not needed now)

`src/a2_analyst/context.py` computes the same since-window for the analyst's
*context* (the LLM just sees `"volume_multiple": null` — no veto, so it was
never part of this incident). If we want the analyst to always see real
volume numbers too, the same maturity arithmetic could gate that computation.
Low value, zero risk deferred.
