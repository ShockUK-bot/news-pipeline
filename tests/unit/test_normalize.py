"""Unit tests: clock discipline, content hash, source normalization.
No database required."""
import pytest
from datetime import datetime, timezone

from common.clock import iso_utc, parse_ts, is_market_hours
from common.contracts import NewsItem, content_hash
from c1_ingestion.normalize import (NormalizeError, normalize_alpaca,
                                    normalize_edgar, normalize_rss)


# ---- clock -------------------------------------------------------------------

def test_parse_ts_iso_z():
    dt = parse_ts("2026-07-07T14:32:11.481Z")
    assert dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0


def test_parse_ts_offset_converts_to_utc():
    dt = parse_ts("2026-07-07T10:32:11-04:00")
    assert dt.hour == 14


def test_parse_ts_rejects_naive():
    with pytest.raises(ValueError):
        parse_ts("2026-07-07T14:32:11")


def test_parse_ts_rejects_garbage():
    for bad in ("", "0000-00-00", "not a date", None):
        with pytest.raises((ValueError, TypeError)):
            parse_ts(bad)


def test_parse_ts_epoch_millis():
    dt = parse_ts(1751898731481)
    assert dt.year == 2025 or dt.year == 2026  # sanity: in-range epoch


def test_iso_utc_format():
    s = iso_utc(datetime(2026, 7, 7, 14, 32, 11, 481000, tzinfo=timezone.utc))
    assert s == "2026-07-07T14:32:11.481Z"


def test_market_hours_weekend_false():
    # Sunday noon ET
    assert not is_market_hours(datetime(2026, 7, 5, 16, 0, tzinfo=timezone.utc))


def test_market_hours_tuesday_true():
    # Tuesday 2026-07-07 14:00 UTC = 10:00 ET
    assert is_market_hours(datetime(2026, 7, 7, 14, 0, tzinfo=timezone.utc))


# ---- content hash --------------------------------------------------------------

def test_hash_ignores_trivial_reformatting():
    a = content_hash("Acme  Corp  Rises", "on   strong earnings")
    b = content_hash("acme corp rises", "on strong earnings")
    assert a == b


def test_hash_changes_on_real_edit():
    assert content_hash("Acme rises 5%") != content_hash("Acme rises 15%")


# ---- alpaca ---------------------------------------------------------------------

ALPACA_OK = {
    "T": "n", "id": 40892639, "headline": "Acme Corp Announces Buyback",
    "summary": "Board approves $2B repurchase.", "author": "B. Writer",
    "created_at": "2026-07-07T14:30:00Z", "updated_at": "2026-07-07T14:30:00Z",
    "symbols": ["acme"], "url": "https://example.com/x", "source": "benzinga",
}


def test_alpaca_ok():
    item = normalize_alpaca(ALPACA_OK)
    assert item.item_id == "alpaca:40892639"
    assert item.source_tier == 2
    assert item.symbols == ["ACME"]              # uppercased
    assert item.published_ts.tzinfo is not None
    assert item.received_ts >= item.published_ts


def test_alpaca_missing_headline_quarantines():
    bad = {**ALPACA_OK, "headline": ""}
    with pytest.raises(NormalizeError) as e:
        normalize_alpaca(bad)
    assert e.value.reason_code == "MISSING_REQUIRED_FIELD"


def test_alpaca_bad_timestamp_quarantines():
    bad = {**ALPACA_OK, "created_at": "0000-00-00"}
    with pytest.raises(NormalizeError) as e:
        normalize_alpaca(bad)
    assert e.value.reason_code == "BAD_TIMESTAMP"


def test_alpaca_symbols_not_list_quarantines():
    bad = {**ALPACA_OK, "symbols": "ACME"}
    with pytest.raises(NormalizeError) as e:
        normalize_alpaca(bad)
    assert e.value.reason_code == "UNKNOWN_SCHEMA"


def test_alpaca_empty_symbols_valid():
    """v0.2: symbols MAY BE EMPTY — untagged items are valid."""
    ok = {**ALPACA_OK, "symbols": []}
    assert normalize_alpaca(ok).symbols == []


# ---- edgar ------------------------------------------------------------------------

EDGAR_OK = {
    "id": "urn:tag:sec.gov,2008:accession-number=0001234567-26-000123",
    "title": "8-K - ACME CORP (0001234567) (Filer)",
    "link": "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000123-index.htm",
    "updated": "2026-07-07T16:45:00-04:00",
    "summary": "Item 2.02 Results of Operations",
}


def test_edgar_ok_8k_channel():
    item = normalize_edgar(EDGAR_OK)
    assert item.item_id == "edgar:0001234567-26-000123"
    assert item.source_tier == 1
    assert "8-K" in item.channels
    assert item.symbols == []                    # CIK != ticker; mapping is not C1's job


def test_edgar_friday_pm_flag():
    # 2026-07-10 is a Friday; 16:45 ET is after close
    entry = {**EDGAR_OK, "updated": "2026-07-10T16:45:00-04:00"}
    assert "friday_pm" in normalize_edgar(entry).channels


def test_edgar_thursday_no_friday_flag():
    entry = {**EDGAR_OK, "updated": "2026-07-09T16:45:00-04:00"}
    assert "friday_pm" not in normalize_edgar(entry).channels


def test_edgar_no_id_quarantines():
    bad = {**EDGAR_OK, "id": "", "link": ""}
    with pytest.raises(NormalizeError) as e:
        normalize_edgar(bad)
    assert e.value.reason_code == "MISSING_REQUIRED_FIELD"


# ---- rss --------------------------------------------------------------------------

RSS_OK = {
    "title": "Acme Corp said to weigh sale to larger rival",
    "id": "https://marketpulse.example/acme-777",
    "link": "https://marketpulse.example/acme-777",
    "published": "2026-07-07T14:29:51Z",
    "summary": "Unconfirmed report of strategic alternatives.",
}


def test_rss_ok():
    item = normalize_rss(RSS_OK, feed_name="marketpulse")
    assert item.item_id.startswith("rss:marketpulse:")
    assert item.source == "rss:marketpulse"
    assert item.source_tier == 3


def test_rss_stable_item_id():
    a = normalize_rss(RSS_OK, feed_name="marketpulse")
    b = normalize_rss(RSS_OK, feed_name="marketpulse")
    assert a.item_id == b.item_id                # same guid -> same id across polls


def test_rss_missing_ts_quarantines():
    bad = {k: v for k, v in RSS_OK.items() if k != "published"}
    with pytest.raises(NormalizeError) as e:
        normalize_rss(bad, feed_name="marketpulse")
    assert e.value.reason_code == "MISSING_REQUIRED_FIELD"


# ---- contract round-trip -------------------------------------------------------------

def test_payload_is_contract_shaped():
    item = normalize_alpaca(ALPACA_OK)
    p = item.payload()
    for key in ("item_id", "revision", "source", "source_tier", "headline",
                "content_hash", "symbols", "published_ts", "received_ts"):
        assert key in p, f"missing contract field {key}"
    assert p["published_ts"].endswith("Z")
    assert "raw" not in p                        # raw stays in the DB, not on the queue

