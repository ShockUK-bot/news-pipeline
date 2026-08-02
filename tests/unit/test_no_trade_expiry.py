"""v0.12.9 unit tests — DB-free: the analyst no-trade verdict at the schema
layer (0 valid, negatives still rejected), prompt coverage of the new rule,
and the A1 scanner staleness guard's age math and fail-open behavior."""
from datetime import datetime, timedelta, timezone

import pytest

from a1_triage.service import scanner_age_minutes
from a2_analyst.schema import ThesisValidationError, validate_thesis

NOW = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)


def _thesis(**over):
    base = {
        "ticker": "ACME",
        "direction": "down",
        "magnitude_est": 0.0,
        "expected_move_window": "30_minutes",
        "horizon": "SHORT",
        "confidence": 0.1,
        "priced_in_assessment": "Move fully exhausted; nothing left.",
        "source_risk": "low",
        "invalidation": {"machine_checkable": [], "news_checkable": []},
        "related_opportunities": [],
        "reason": "Stale mover with no remaining driver.",
    }
    base.update(over)
    import json
    return json.dumps(base)


# ------------------------------------------------------------- no-trade schema

def test_magnitude_zero_is_now_valid():
    """The 2026-08-02 incident shape: an honest 'nothing left' must parse."""
    t = validate_thesis(_thesis())
    assert t.magnitude_est == 0.0


def test_negative_magnitude_still_rejected():
    with pytest.raises(ThesisValidationError):
        validate_thesis(_thesis(magnitude_est=-0.01))


def test_upper_bound_unchanged():
    with pytest.raises(ThesisValidationError):
        validate_thesis(_thesis(magnitude_est=0.51))
    assert validate_thesis(_thesis(magnitude_est=0.5)).magnitude_est == 0.5


def test_positive_magnitude_unchanged():
    assert validate_thesis(_thesis(magnitude_est=0.03)).magnitude_est == 0.03


def test_prompt_teaches_the_zero_verdict():
    from a2_analyst import prompt as p
    src = open(p.__file__).read()
    assert src.count("magnitude_est to 0") >= 2, \
        "both the news and scanner rule blocks must teach the 0 verdict"


# ------------------------------------------------------- scanner staleness age

def test_age_math():
    scanner = {"detected_ts": (NOW - timedelta(minutes=30)).isoformat()}
    assert scanner_age_minutes(scanner, NOW) == pytest.approx(30.0)


def test_week_old_signal_from_the_outage():
    scanner = {"detected_ts": (NOW - timedelta(days=6)).isoformat()}
    assert scanner_age_minutes(scanner, NOW) > 15


def test_fresh_signal_under_threshold():
    scanner = {"detected_ts": (NOW - timedelta(minutes=2)).isoformat()}
    assert scanner_age_minutes(scanner, NOW) < 15


def test_missing_or_bad_ts_fails_open():
    assert scanner_age_minutes({}, NOW) is None
    assert scanner_age_minutes({"detected_ts": ""}, NOW) is None
    assert scanner_age_minutes({"detected_ts": "not a time"}, NOW) is None
    # naive timestamps are rejected by parse_ts -> None, not a crash
    assert scanner_age_minutes({"detected_ts": "2026-08-03T14:00:00"},
                               NOW) is None
