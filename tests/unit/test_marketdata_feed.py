"""v0.5.9 unit tests: Alpaca feed selection via ALPACA_FEED. No network."""
import pytest

from common.marketdata import AlpacaData


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("ALPACA_KEY_ID", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")


def test_default_feed_is_sip(monkeypatch):
    monkeypatch.delenv("ALPACA_FEED", raising=False)
    assert AlpacaData()._feed == "sip"


def test_explicit_iex_fallback(monkeypatch):
    monkeypatch.setenv("ALPACA_FEED", "iex")
    assert AlpacaData()._feed == "iex"


def test_case_and_whitespace_tolerated(monkeypatch):
    monkeypatch.setenv("ALPACA_FEED", "  SIP ")
    assert AlpacaData()._feed == "sip"


def test_invalid_feed_rejected_loudly(monkeypatch):
    monkeypatch.setenv("ALPACA_FEED", "polygon")
    with pytest.raises(RuntimeError):
        AlpacaData()
