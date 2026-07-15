"""Suite-wide guard (added 2026-07-14 after the production-DB test incident):
integration fixtures TRUNCATE tables, so pytest refuses to run unless
PIPELINE_DSN points at a database whose name ends in _test."""
import os
import pytest


def pytest_configure(config):
    dsn = os.environ.get("PIPELINE_DSN", "")
    db = dsn.rsplit("/", 1)[-1].split("?")[0] if dsn else ""
    if db and not db.endswith("_test"):
        pytest.exit(
            f"REFUSING TO RUN: PIPELINE_DSN points at '{db}', not a *_test "
            "database. Integration fixtures TRUNCATE tables. Use "
            "trading_test.", returncode=2)
