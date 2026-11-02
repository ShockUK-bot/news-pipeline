# Patch notes v0.29.0 (2026-11-02): dilution short lane live at half risk; exhaustion fade named and watched

Third release of 2026-11-02, from sections 5.2 and 5.4 of `docs/claude_october-loss-review-2026-11-02.md`.

## Dilution shorts (live, small)

Plain news shorts stayed off (v0.27.0). One class of bearish news has shown an edge in the gate's own counterfactuals: equity offerings, registered directs, private placements, at the market programs, convertibles and reverse splits announced by the issuer. 193 analyst down calls on such headlines since 2026-09-01: 59 percent lower by the close, mean -2.74 percent, median -1.0. Most were vetoed for borrow (correct) or for having only one outlet (wrong: the issuer's release is the primary source).

- `src/c3_gate/rules.py` `catalyst_class(headline)`: a deterministic regex, returns `offering` or None. `lane_for` routes a down thesis on an offering headline to the new `news_short_dilution` lane. `credibility_with_class` lowers the outlet requirement to one for classes in `gate.credibility_exempt_classes`. `evaluate` takes `cclass`.
- `src/c3_gate/service.py` reads the headline with the item, computes the class, picks the lane, passes it to the short context and to `evaluate`, and puts `lane` and `catalyst_class` in the gate PASS body.
- `config/shorting.yaml` `lanes.news_short_dilution: true` (`news_short` stays false; `etb_only` still applies, so unborrowable micro caps are still vetoed). `config/gate.yaml` `credibility_exempt_classes: [offering]`. `config/risk.yaml` `news.short_dilution_risk_multiplier: 0.5`; `src/a3_risk/sizing.py` `news_capital_cfg` applies it by lane (`src/a3_risk/service.py` passes `gate.lane`).
- Everything else about the trade is the existing short mechanics: open handoff or intraday confirmation, SSR veto, `short_term_v1` exits, the 15:45 overnight rule, the A9 lane kill rule (10 trades at -3 R or worse).

## Exhaustion fade: measured, not yet live

On a second look the fade result is one strong month (October, +20.8 R over 185 replayed candidates) and one flat month (September, +1.3 R over 110), and it is concentrated in two sub buckets. Going live on the aggregate today would repeat the mistake that built the news and thesis lanes. Instead the two buckets are named and A9 holds a bar on each:

- `up_structure`: up movers the scanner structure rule vetoed, shorted (October +5.7 R over 31).
- `down_capped`: down movers the per hour, per scan, concurrency or early window caps or the size clip kept out, bought (October +13.5 R over 59).

`src/a11_metrics/funnel.py` `summary` returns both; `src/a9_review/candidates.py` `rule_fade_up_structure` and `rule_fade_down_capped` watch them and propose a live lane at 30 rows, +6 R and 55 percent winners in the four week window. No new code is needed to measure them: the funnel already replays every one.

## Files and tests

`config/shorting.yaml`, `config/gate.yaml`, `config/risk.yaml`, `src/c3_gate/rules.py`, `src/c3_gate/service.py`, `src/a3_risk/sizing.py`, `src/a3_risk/service.py`, `src/a11_metrics/funnel.py`, `src/a9_review/candidates.py`, `tests/unit/test_v0_29_0.py` (one v0.27.0 wiring pin updated). Version 0.29.0 (v0.29.1 fixes a stale test pin; code identical). Suite 935 passed.

## Services

Deployed together with v0.27.0 and v0.28.0 after the close on 2026-11-02: restart `c3-gate`, `a3-risk`, `c4-exec`, `c10-scanner` (stop `c7-watchdog.timer` first, start it after). Timers (`a11-metrics`, `a9-review`, `thesis-entry`) pick up the code on their next run. Rollback: `git reset --hard v0.26.4` and restart the same four services; v0.28.0 and v0.27.0 are intermediate tags if only part needs undoing.
