# Patch notes v0.26.4 (2026-09-30): a filled catastrophe stop no longer blinds the engine or vanishes from the journal

## Incident (LITE, 2026-09-29)

Scanner long, 29 shares at 995.45 at 08:56 CT. The stock fell to 966 within nine minutes; the broker resident catastrophe stop (971.73) filled 29 at 971.24 at 09:03:21. Twenty four seconds later the engine's own stop fired on the minute bar and, as its first step, tried to cancel the catastrophe order. Alpaca answers 422 to cancelling a filled order; `AlpacaBroker.cancel` raised on it, the exception aborted the entire engine pass, and it did so every minute until 09:16, so INVX, FRMI, RIOT, BRX and MDB had no synthetic stop checks for 13 minutes (their broker stops stayed in place). At 09:17 the periodic reconcile found LITE missing at the broker and marked it CLOSED_EXTERNAL with realised 0 and no exit row. The real loss, 702.23, was absent from the journal, the evening digest, the rollups and A11.

## Fixes

- `src/common/broker.py` `cancel`: 422 is treated like 404, "not cancellable", so the existing path (read the order, if filled journal a CATASTROPHE exit) runs. 5xx still raises.
- `src/c4_exec/service.py` engine loop: each position's halt check, bar fetch and step run in their own try; a failure is logged with the position and the pass continues with the next name.
- `src/c4_exec/reconcile.py`: a local position missing at the broker is first checked for a filled catastrophe order; if found, a CATASTROPHE exit is recorded with the fill price, P&L, R multiple and slippage (`RECONCILE_CATASTROPHE_FILL` audit), instead of the bare external close. The external close remains for genuinely unexplained cases.
- `tests/unit/test_v0_26_4.py`. Suite 916 passed.

## Backfill for LITE (needs the operator's approval; see the session summary)

Exit row, position P&L and last price, an EXIT event, and removal of the zero valued trade metrics row so A11 recomputes it.

## Services

`c4-exec` restart required (broker cancel and loop isolation live in the process). Rollback: `git reset --hard v0.26.3` and restart `c4-exec`.
