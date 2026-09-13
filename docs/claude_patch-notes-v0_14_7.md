# Patch notes v0.14.7

Cleanup release, 2026-09-13. Tests only. No runtime code, config or schema changed, so **no service restart**. Previous tag: `v0.14.6`. The suite had carried three failures since before v0.14.5; it is now green.

## Fixes

1. `tests/unit/test_a7_c5.py::test_render_busy_day_with_narrative`. Stale assertion: since v0.14.3 the end of day report's position line carries the side (`OPENED LONG ACME 50 @ $100.00`). The test still expected the pre v0.14.3 shape. Assertion updated; the renderer is correct.

2. `tests/unit/test_triage_v047.py::test_confidence_required`. Stale input: since v0.12.4 `tickers`, `direction_hint`, `urgency` and `novelty_score` are required too, and `validate_triage` reports only the first four schema errors. With five fields missing, `confidence` was the fifth and fell off the message. The test now supplies the other four so `confidence` is the only missing field, which is what the test was written to prove. The schema is correct.

3. `tests/unit/test_cik_map.py::test_end_to_end_stored_with_symbols`. This is a database integration test (it writes to `news.news_items` and `queue.messages`) living in the unit folder. `tests/conftest.py` already refuses to run against any database not named `*_test`, and the unit command in `CLAUDE.md` unsets `PIPELINE_DSN`, so the test could never pass here. It is now marked `skipif` when `PIPELINE_DSN` is unset, with the reason pointing at the conftest rule. The test itself is unchanged and runs as before against a `trading_test` database.

## Result

`env -u PIPELINE_DSN .venv/bin/python -m pytest tests/unit -q`: **787 passed, 1 skipped**. The 8 remaining warnings are `@pytest.mark.asyncio` module marks on synchronous tests in `test_cik_map.py`; harmless, left alone.

## Files touched

`tests/unit/test_a7_c5.py`, `tests/unit/test_triage_v047.py`, `tests/unit/test_cik_map.py`, `pyproject.toml` (0.14.6 to 0.14.7), `CLAUDE.md` (version line), `docs/claude_current-state-2026-09-13b.md` (open item closed).

## Services

None restarted. Running services are on v0.14.6 code, which is byte for byte the same as v0.14.7 for every runtime file. `git describe --tags` says v0.14.7; the services' journaled config version stays `ea856e3` (v0.14.6) until their next restart, which is expected.

## Rollback

Not applicable (no runtime change). To drop the tag: `git reset --hard v0.14.6` on `main`.
