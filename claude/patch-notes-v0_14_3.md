# v0.14.3 — position lines say their side; horizon is a timeframe (2026-08-24)

Reported problem: the 2026-08-24 EOD email said "opened a short position on
DSGX"; the journal says side=LONG (stop below entry, coherent long anatomy;
dashboard was right). Root cause: A7's opened-today facts query never
selected p.side (missed by the v0.13.0 side-awareness sweep), and 'horizon'
values are the bare enum SHORT/LONG — the heavy-slot narrative model read
{"horizon": "SHORT"} with no side and wrote the reasonable-but-wrong
sentence. The deterministic render printed the same bare enum next to a
stop price (render.py:67).

Fix, at the source so model and template paths both inherit it:
- a7 facts: opened rows gain side; all horizon values map SHORT->SHORT_TERM,
  LONG->LONG_TERM (open_positions already had side)
- a8 facts: same horizon mapping (side was already present)
- a7/a8 render: side printed explicitly on every position line
  ("OPENED LONG DSGX 100 @ ...", "DSGX [LONG SHORT_TERM] ...")

4 files: src/a7_report/facts.py, src/a7_report/render.py,
src/a8_briefing/facts.py, src/a8_briefing/render.py. No config, no schema,
no restarts (oneshot timers). Verification: 08-25 briefing 07:35 ET shows
"[LONG SHORT_TERM]" on the DSGX line; 08-25 EOD shows sides on all
position lines and no side/timeframe confusion in the narrative.

Follow-ups noted, not in this tag: unit test pinning "side present + no
bare horizon enum" in both facts builders (needs test fixtures for the DB
rows); same-ambiguity audit for a6/a12/a13 prompt contexts (v0.13.0
claimed side-awareness there — trust but verify, this path claimed it too).
