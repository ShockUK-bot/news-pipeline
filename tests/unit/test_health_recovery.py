"""Unit tests for the v0.11.11 health-recovery fixes.

Both the C8 regime loop and the C1 RSS aggregate row used to set their
`journal.health` component to DEGRADED on failure but never write OK again
after recovering, so a single transient blip latched the dashboard red until
the service was restarted. These tests pin the new behaviour of the two pure
decision helpers: reset-to-OK on recovery, and degrade only after N
consecutive failures. No database or network is touched.
"""
from c8_regime.service import regime_health
from c1_ingestion.sources.rss import aggregate_health


# --- regime -----------------------------------------------------------------

def test_regime_ok_on_success():
    assert regime_health(0, 2, regime_id=207) == ("OK", "snapshot 207")


def test_regime_ok_with_no_id_at_startup():
    assert regime_health(0, 2) == ("OK", "snapshot loop running")


def test_regime_single_failure_is_transient():
    # one failure, threshold 2 -> leave the row untouched (no false red light)
    assert regime_health(1, 2, last_error="ConnectError('All connection attempts failed')") is None


def test_regime_degrades_after_threshold():
    status, detail = regime_health(2, 2, last_error="ConnectError('boom')")
    assert status == "DEGRADED"
    assert "ConnectError" in detail and "x2" in detail


def test_regime_recovers_to_ok_after_degrade():
    # the very next success resets fails to 0 -> OK, clearing the latch
    assert regime_health(0, 2, regime_id=208)[0] == "OK"


# --- rss aggregate ----------------------------------------------------------

def test_rss_all_ok():
    fok = {"prnewswire-news": True, "globenewswire-public": True, "businesswire-all": True}
    assert aggregate_health(fok, 0, 2, 60) == ("OK", "3 feeds, every 60s")


def test_rss_one_down_still_ok():
    fok = {"prnewswire-news": True, "globenewswire-public": False, "businesswire-all": True}
    assert aggregate_health(fok, 0, 2, 60) == ("OK", "2/3 feeds OK")


def test_rss_all_down_one_pass_is_transient():
    fok = {"a": False, "b": False, "c": False}
    # streak 1, threshold 2 -> don't flip yet (this is what latched yesterday)
    assert aggregate_health(fok, 1, 2, 60) is None


def test_rss_all_down_degrades_after_threshold():
    fok = {"a": False, "b": False, "c": False}
    status, detail = aggregate_health(fok, 2, 2, 60)
    assert status == "DEGRADED" and detail == "all feeds failing (x2)"


def test_rss_recovers_to_ok_when_a_feed_returns():
    fok = {"a": False, "b": True, "c": False}
    # one feed back -> aggregate immediately OK again (old latch cleared)
    assert aggregate_health(fok, 0, 2, 60) == ("OK", "1/3 feeds OK")


def test_rss_empty_feeds_noop():
    assert aggregate_health({}, 5, 2, 60) is None
