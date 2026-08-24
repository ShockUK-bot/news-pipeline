# v0.14.2 — narrative ceilings raised for Qwen3.8 verbosity (2026-08-24)

Config only, six values, no code, no restarts (oneshot timers re-read).

Qwen3.8 writes ~15%+ longer prose than Qwen3.6; ceilings sized for the old
model now truncate mid-string. Day-one evidence: A8's 07:35 briefing shipped
narrative=False after two Unterminated-string retries at max_tokens 900
(slot=analyst); A4's ranking has truncated at 1400 vs top_k 15 on every run
since at least Aug 12 (chars 3448/3707/3716), costing 3-5 min of heavy time
per rescue-retry, with one fallback ranking (Aug 12).

- a4 ranking: max_tokens 1400 -> 4000, timeout_secs 420 -> 720 (heavy slot
  decodes ~4000 tok in ~450s; old timeout would kill the fix)
- a7 EOD narrative: 900 -> 2000, timeout 300 -> 600
- a8 briefing narrative: 900 -> 2000 (240s timeout holds on analyst slot)
- a6 nightly: 900 -> 1800; per-position call 700 -> 1200

Ceilings, not targets — generation stops when the JSON closes. Verification:
tonight's a6-nightly (20:00 ET), tomorrow's a4-premarket 07:00 ET (ranking
succeeds FIRST TRY, no invalid-JSON warning) and a8 briefing 07:35 ET
(narrative=True, opening summary present in the email).

Known related, deferred to v0.14.3 pending A7 log evidence: the EOD email
called the DSGX LONG a "short position" — horizon/side vocabulary collision
(horizon=SHORT means short-TERM). Position itself verified correct (side
LONG, stop below entry, coherent anatomy).
