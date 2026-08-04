# v0.12.12 — GATE LAB dashboard tab (2026-08-03)

Operator question: "can I only see the results using psql?" Until now,
yes — the v0.12.10 counterfactuals and v0.12.11 shadow results lived only
in the database (the decision tape shows individual WOULD_TRADE/veto rows
scrolling by, but nothing aggregates them). This release adds a fourth
dashboard tab, **GATE LAB**, next to LIVE / HISTORY / PERFORMANCE:

1. **Today at the gate** — every gate action today with its count and the
   average evaluation minute. Avg minutes climbing above ~6 is the
   v0.12.10 re-check window visibly working (final verdicts near the
   30-minute mark instead of one look at minute 4).
2. **Veto counterfactuals (regular hours, last 14 days)** — per veto
   reason: how many, how many measured, the average best move after the
   veto, and the average move to the close. This is the §14
   threshold-tuning scoreboard: a big green "avg best %" on
   GATE_NO_CONFIRM means the gate is leaving money on the table; near
   zero means the vetoes are earning their keep.
3. **Extended-hours shadow — outcome mix** — WOULD_TRADE vs each veto
   reason during pre/post sessions.
4. **Shadow would-trades** — each hypothetical EH entry (at the ask, no
   order placed) with its entry-to-close, best, and worst outcome; rows
   show "measuring…" until the post-close sweep fills them.

Backend: one new read-only endpoint `GET /api/gatelab` (basic-auth like
everything else, fetched on tab open + every 30 s, not in the WS push, so
it adds zero steady-state load). No new tables; reads
journal.gate_counterfactuals (migration 010) and journal.decisions.
Verified end-to-end against a live PG16 with migration 010 applied.

## Files

REPLACED (2): `dashboard/app.py`, `dashboard/index.html`
NEW (2): these patch notes, the deploy guide

No schema/env/config/unit changes. Compatible with the chat-tab variant
(app_chat.py injects at `</body>`, which is untouched).

## Requires

v0.12.10 deployed (migration 010 — the endpoint reads that table).
v0.12.11 recommended (fills the EH panels; without it they just show
"no extended-hours evaluations yet").

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.11
sudo systemctl restart c6-dashboard
```
