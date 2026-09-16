# ops/research

One-off research scripts, kept so a result can be rerun. Read only against the
journal; market data from Alpaca's historical bars API (needs the pipeline env
sourced and `PYTHONPATH=src`). Data and outputs go to `$RESEARCH_DIR`
(default `/tmp/research`); create it first.

- `onepct_firsthit.py` + `onepct_report.py`: for every scanner detection and
  gate verdict of the last 30 days, which comes first after detection, +1%
  or -1% (and variants), by feature. Written for the "1% gain" design,
  2026-09-15.
- `burst_backtest.py`: 1-minute burst rules on a liquid universe
  (`$RESEARCH_DIR/universe.txt`, one ticker per line), bracket + time stop +
  cost. `burst_context.py` splits those trades by news / scanner / bare
  context (needs `escalations.txt` and `scanner_rows.txt` exports, see the
  design memo for the queries).
- `escalation_burst_backtest.py`: enter on A1 escalations in the hinted
  direction, with and without a burst confirmation.

See `docs/claude_1pct-gain-design-2026-09-15.md` for the results.
