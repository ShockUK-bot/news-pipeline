# Patch notes v0.26.1 (2026-09-23): exits priced from the live quote; dashboard shows the current stop

Found on the first morning of the v0.26.0 profit lock. FRMI (open since 09-02, high water mark 5.91 from earlier in September) was ratcheted to 5.61 at 08:31 CT by the lock, above the 4.91 price, so a TRAIL exit fired at once. That exit then went unfilled three times in a row: the sell limit was priced 30 bps under the LAST trade (4.914) while the live quote was 4.88 bid / 4.89 ask, so it sat above the bid, was cancelled after 45 s, the catastrophe stop was re-placed, and the cycle repeated every two minutes. Separately, the dashboard's Stop column had always read the initial stop, so no breakeven, trail or lock ratchet was ever visible there.

- `src/c4_exec/engine.py` `_exit_price`: exits and scale outs are priced 20 bps through the live bid (long) or ask (short) from the market data snapshot, falling back to the bar's bid or ask or the last close with a 10 bps concession when no quote is reachable. `src/c4_exec/service.py` wires `engine.quote_fn = marketdata.snapshot`. Needs a `c4-exec` restart: deployed in the evening window (not during the session).
- `schema/migrations/022-dash-positions-current-stop.sql` (applied 08:55 CT): `dash_positions.stop_price` is the current stop with the initial as fallback; `stop_basis` and `initial_stop` appended. `dashboard/index.html` shows the basis beside the stop. `c6-dashboard` restarted.
- Note on the lock's first application: for positions opened before the lock existed it uses the journaled high water mark, which for FRMI meant "you were up 27 percent once; you are up 5 now; sell". That is the rule as specified (lock gains), applied late; new positions arm it from their own path.
- `tests/unit/test_v0_26_1.py`. Suite 911 passed.

Rollback: `git reset --hard v0.26.0` on main, restart `c4-exec` and `c6-dashboard`; the view can stay.
