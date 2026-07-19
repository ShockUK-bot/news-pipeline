"""Earnings-calendar unit tests — DB-free: Alpha Vantage CSV parsing
(including the HTTP-200 JSON error trap), conservative session math over
the real NYSE calendar (weekend + holiday rolls)."""
from datetime import date

import pytest

from c1_ingestion.earnings import parse_alphavantage_csv, sessions_until

CSV = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency\r\n"
       "ACME,Acme Corp,2026-07-21,2026-06-30,1.23,USD\r\n"
       "NVDA,NVIDIA,2026-08-26,2026-07-31,,USD\r\n"
       "BAD,Broken Row,not-a-date,2026-06-30,0.5,USD\r\n"
       ",No Symbol,2026-07-22,2026-06-30,0.5,USD\r\n")


def test_parse_valid_rows_and_skips_garbage():
    rows = parse_alphavantage_csv(CSV)
    assert [r["ticker"] for r in rows] == ["ACME", "NVDA"]
    assert rows[0]["report_date"] == date(2026, 7, 21)
    assert rows[0]["eps_estimate"] == pytest.approx(1.23)
    assert rows[1]["eps_estimate"] is None          # empty estimate ok
    assert rows[0]["fiscal_ending"] == date(2026, 6, 30)


def test_parse_rejects_json_error_body():
    # Alpha Vantage answers bad keys / rate limits as HTTP-200 JSON
    with pytest.raises(ValueError):
        parse_alphavantage_csv('{"Information": "rate limit reached"}')
    with pytest.raises(ValueError):
        parse_alphavantage_csv("")


def test_sessions_until_today_and_next_session():
    # Tue 2026-07-07 reporting Tue = 0 sessions; Wed = 1 session
    assert sessions_until(date(2026, 7, 7), date(2026, 7, 7)) == 0
    assert sessions_until(date(2026, 7, 7), date(2026, 7, 8)) == 1
    assert sessions_until(date(2026, 7, 7), date(2026, 7, 9)) == 2


def test_sessions_until_weekend_roll():
    # Fri 2026-07-10 -> Mon 2026-07-13 is the next session
    assert sessions_until(date(2026, 7, 10), date(2026, 7, 13)) == 1


def test_sessions_until_holiday_roll():
    # July 4 2026 falls on a Saturday -> NYSE observes Friday 2026-07-03.
    # From Thu 2026-07-02, Monday 2026-07-06 is therefore ONE session away.
    assert sessions_until(date(2026, 7, 2), date(2026, 7, 6)) == 1


def test_sessions_until_past_report_clamps_to_zero():
    assert sessions_until(date(2026, 7, 7), date(2026, 7, 1)) == 0
