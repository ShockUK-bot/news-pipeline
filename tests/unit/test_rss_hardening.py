"""Unit tests for the v0.13.3 RSS hardening.

Three problems, one release:

  1. The poller had a flat 20s connect+read timeout and no retry, so one
     slow body from globenewswire became a failed poll, an ERROR line, and a
     yellow dashboard row.
  2. The per-feed health row went DEGRADED on the FIRST miss — the last
     place in C1 that still lacked the transient tolerance v0.11.7 (EDGAR)
     and v0.11.11 (the RSS aggregate) already have.
  3. A 200 OK does not mean you got the feed you asked for. Probing the live
     wires on 2026-08-15 found three silent failures a status check cannot
     see, all three reproduced below as tests.

Plus the nasdaq_halts adapter, without which every item of that feed would
be quarantined for having no <guid> and no <link>.

Pure functions only — no database, no network, no clock of their own.
"""
import httpx
import pytest
import yaml

from datetime import datetime, timedelta, timezone
from pathlib import Path

from c1_ingestion.normalize import NormalizeError, normalize_rss
from c1_ingestion.sources.rss import (
    DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT, FeedContentError,
    adapt_entry, content_errors, feed_health, is_retryable, newest_published,
    retry_delay, staleness, symbols_from, timeout_for,
)

NOW = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)

SOURCES = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"
BROWSER_UA_MARKERS = ("Chrome/", "Safari/", "AppleWebKit/", "Gecko/")


def _rss_block() -> dict:
    return yaml.safe_load(SOURCES.read_text())["rss"]


def _rss_feeds() -> dict:
    return {f["name"]: f for f in _rss_block()["feeds"]}


# --- config registration ----------------------------------------------------
# v0.13.4: these exist because v0.13.3 renamed a feed and the breakage
# surfaced in test_macro.py on the Spark mid-deploy instead of here. Config
# that other tests assert on by name is an interface; it gets its own tests.

def test_dead_prnewswire_all_news_feed_is_gone():
    """PR Newswire's all-news feed 404s permanently (2026-08-15) and so does
    the v0.11.2 alternate. Nothing may point at either again."""
    feeds = _rss_feeds()
    assert "prnewswire-news" not in feeds
    urls = " ".join(f["url"] for f in feeds.values())
    assert "news-releases-list.rss" not in urls
    assert "all-news-releases-from-PR-newswire-news" not in urls


def test_prnewswire_category_feeds_assert_their_channel_title():
    """Without expect_title_prefix we would silently ingest PR Newswire's
    fallback feed forever — it answers 200 with 20 fresh items."""
    feeds = _rss_feeds()
    for name in ("prnewswire-financial", "prnewswire-bustech"):
        assert name in feeds, name
        assert feeds[name].get("expect_title_prefix"), name


def test_globenewswire_subject_lanes_assert_non_empty():
    """An unknown GlobeNewswire subject code returns a valid EMPTY feed, so
    every subjectcode lane must carry require_items or a typo is silent."""
    for name, feed in _rss_feeds().items():
        if "subjectcode" in feed["url"]:
            assert feed.get("require_items"), name


def test_globenewswire_subject_labels_are_url_safe():
    """A label containing "/" or "'" returns HTTP 400 unless double-encoded;
    the numeric code alone selects the content, so labels stay cosmetic."""
    for name, feed in _rss_feeds().items():
        if "subjectcode" in feed["url"]:
            code = feed["url"].split("subjectcode/", 1)[1].split("/", 1)[0]
            assert "'" not in code and "%" not in code, name


# --- the Akamai tarpit (v0.13.4) --------------------------------------------
# Probed from the Spark 2026-08-15: with the Chrome UA v0.11.1 introduced,
# EVERY globenewswire.com URL completed TLS and then received ZERO bytes until
# the client gave up. With curl's, httpx's, or an honest news-pipeline string:
# HTTP 200 in under a quarter of a second. Sending NO User-Agent also hangs.
# So it is browser impersonation being refused, not bots as such.

def test_every_globenewswire_feed_overrides_the_browser_ua():
    feeds = _rss_feeds()
    gnw = {n: f for n, f in feeds.items() if "globenewswire.com" in f["url"]}
    assert gnw, "expected globenewswire feeds in the registry"
    for name, feed in gnw.items():
        ua = feed.get("user_agent", "")
        assert ua, f"{name} would be tarpitted by Akamai without its own UA"
        assert not any(m in ua for m in BROWSER_UA_MARKERS), name


def test_no_feed_override_impersonates_a_browser():
    """A per-feed override exists to escape browser impersonation. One that
    impersonates a browser is the bug it was added to fix."""
    for name, feed in _rss_feeds().items():
        ua = str(feed.get("user_agent", ""))
        assert not any(m in ua for m in BROWSER_UA_MARKERS), name


def test_only_the_expected_feeds_override_the_user_agent():
    """Three publishers, three incompatible demands (prnewswire needs the
    browser string, BLS needs contact info, GlobeNewswire needs anything but
    a browser). Pin exactly who deviates so a fourth is a deliberate act."""
    feeds = _rss_feeds()
    overriding = {n for n, f in feeds.items()
                  if "user_agent" in f or "user_agent_env" in f}
    expected = {"bls-latest"} | {n for n, f in feeds.items()
                                 if "globenewswire.com" in f["url"]}
    assert overriding == expected


def test_bls_user_agent_still_comes_from_the_environment():
    """Rule 22: that string contains an email address; it must never be a
    literal in this public file, even now that literals are in use nearby."""
    bls = _rss_feeds()["bls-latest"]
    assert bls.get("user_agent_env") == "BLS_USER_AGENT"
    assert "@" not in str(bls.get("user_agent", ""))


# --- the businesswire lesson (v0.13.5) --------------------------------------
# businesswire-all answered 200 with a valid, parseable, EMPTY feed — whose
# own <description> said "The RSS channel you requested was deactivated by
# the administrator" — and sat GREEN on the dashboard for an unknown length
# of time, because v0.13.3 shipped require_items on nine feeds and left it
# off the one feed already flagged as fragile. These tests make that
# judgement call impossible to repeat.

def test_dead_businesswire_channel_is_gone():
    feeds = _rss_feeds()
    assert "businesswire-all" not in feeds
    assert "businesswire.com" not in " ".join(f["url"] for f in feeds.values())


def test_every_feed_asserts_non_empty_except_the_halt_feed():
    """A 200 with zero items must never read as healthy. The ONLY feed
    allowed to be legitimately empty is nasdaq-halts, where empty means
    nothing is halted — which is the good outcome, not a fault."""
    unguarded = {n for n, f in _rss_feeds().items() if not f.get("require_items")}
    assert unguarded == {"nasdaq-halts"}


def test_feeds_with_a_publishing_cadence_declare_a_staleness_limit():
    """Every feed that publishes on a predictable rhythm must say so, so a
    frozen-but-healthy feed (the WSJ failure mode) surfaces. Exempt:
    nasdaq-halts (quiet is good) and fed-monetary (FOMC statements are weeks
    apart by design, so a limit would alarm on normal silence)."""
    exempt = {"nasdaq-halts", "fed-monetary"}
    missing = {n for n, f in _rss_feeds().items()
               if n not in exempt and not f.get("stale_after_hours")}
    assert missing == set()


def test_nasdaq_halts_feed_is_wired_for_symbols():
    feed = _rss_feeds()["nasdaq-halts"]
    assert feed["adapter"] == "nasdaq_halts"
    assert feed["symbol_fields"] == ["ndaq_issuesymbol"]
    assert feed["tier"] == 1                      # the exchange, not an aggregator
    # A halt-free session is normal and must never look like a broken feed.
    assert not feed.get("require_items")
    assert not feed.get("stale_after_hours")


def test_transport_settings_keep_the_cycle_inside_the_gap_threshold():
    """The dead-man ladder must not be able to fire because of retries:
    feeds x attempts x connect_timeout has to stay well under the market
    gap threshold."""
    rss = _rss_block()
    worst = (len(rss["feeds"]) * (rss["retries"] + 1)
             * rss["connect_timeout_secs"])
    assert worst < rss["gap_threshold_market_secs"] / 2


def test_read_timeout_is_longer_than_connect_timeout():
    rss = _rss_block()
    assert rss["read_timeout_secs"] > rss["connect_timeout_secs"]


# --- per-feed health tolerance ----------------------------------------------

def test_feed_ok_on_success():
    assert feed_health(0, 3) == ("OK", "polled")


def test_feed_single_failure_is_transient():
    # THE globenewswire ReadTimeout case: one miss must not paint the row.
    assert feed_health(1, 3, last_error="ReadTimeout('')") is None


def test_feed_second_failure_still_transient():
    assert feed_health(2, 3, last_error="ReadTimeout('')") is None


def test_feed_degrades_after_threshold():
    status, detail = feed_health(3, 3, last_error="ReadTimeout('')")
    assert status == "DEGRADED"
    assert "ReadTimeout" in detail and "x3" in detail


def test_feed_recovers_to_ok():
    assert feed_health(0, 3, last_error="ReadTimeout('')") == ("OK", "polled")


def test_feed_stale_is_degraded_but_only_on_a_successful_poll():
    status, detail = feed_health(0, 3, stale="newest item 45.0d old")
    assert status == "DEGRADED" and detail.startswith("stale: ")


def test_feed_failure_detail_beats_stale_detail():
    # An unreachable feed is stale by definition; say the useful thing.
    status, detail = feed_health(3, 3, last_error="ConnectError('boom')", stale="whatever")
    assert "ConnectError" in detail and "stale" not in detail


# --- timeouts ---------------------------------------------------------------

def test_timeout_defaults_are_split_not_flat():
    t = timeout_for({}, {"name": "x", "url": "u"})
    assert t.connect == DEFAULT_CONNECT_TIMEOUT
    assert t.read == DEFAULT_READ_TIMEOUT
    assert t.read > t.connect        # the whole point: reading may be slow


def test_timeout_block_level_override():
    t = timeout_for({"read_timeout_secs": 45}, {"name": "x"})
    assert t.read == 45.0


def test_timeout_feed_overrides_block():
    t = timeout_for({"read_timeout_secs": 25}, {"name": "gnw", "read_timeout_secs": 40})
    assert t.read == 40.0 and t.connect == DEFAULT_CONNECT_TIMEOUT


def test_timeout_junk_config_falls_back_to_default():
    t = timeout_for({"read_timeout_secs": "soon"}, {"name": "x", "connect_timeout_secs": -3})
    assert t.read == DEFAULT_READ_TIMEOUT and t.connect == DEFAULT_CONNECT_TIMEOUT


# --- retry classification ---------------------------------------------------

def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/feed.rss")
    return httpx.HTTPStatusError("", request=request,
                                 response=httpx.Response(code, request=request))


@pytest.mark.parametrize("code", [403, 404, 408, 429, 500, 502, 503, 504])
def test_retryable_statuses(code):
    # 404 included on purpose: v0.11.2 proved prnewswire's 404s were transient.
    assert is_retryable(_status_error(code)) is True


@pytest.mark.parametrize("code", [400, 401, 410, 418])
def test_non_retryable_statuses(code):
    assert is_retryable(_status_error(code)) is False


def test_retryable_transport_errors():
    assert is_retryable(httpx.ReadTimeout("read timed out")) is True
    assert is_retryable(httpx.ConnectError("no route")) is True


def test_content_failure_is_not_retryable():
    # A wrong-title feed will still be wrong-title 1.5s later.
    assert is_retryable(FeedContentError("channel title is EMPTY")) is False


def test_retry_delay_backs_off():
    assert retry_delay(1, 1.5) == 1.5
    assert retry_delay(2, 1.5) == 3.0


# --- content assertions (the three silent failures) -------------------------

def test_prnewswire_fallback_feed_is_caught():
    """A made-up prnewswire category returns valid RSS with 20 fresh items
    and an EMPTY channel title. This is the only tell."""
    feed = {"name": "prnewswire-financial",
            "expect_title_prefix": "All Financial Services"}
    problems = content_errors(feed, {"title": ""}, 20)
    assert problems and "EMPTY" in problems[0]


def test_prnewswire_wrong_category_is_caught():
    feed = {"name": "prnewswire-financial",
            "expect_title_prefix": "All Financial Services"}
    assert content_errors(feed, {"title": "All Sports"}, 20)


def test_prnewswire_right_category_passes():
    feed = {"name": "prnewswire-financial",
            "expect_title_prefix": "All Financial Services"}
    assert content_errors(feed, {"title": "All Financial Services & Investing"}, 20) == []


def test_globenewswire_unknown_subject_code_is_caught():
    """An unknown GlobeNewswire code does not 404 — it returns a valid,
    EMPTY feed echoing whatever feedTitle you asked for."""
    assert content_errors({"name": "gnw-earnings", "require_items": True},
                          {"title": "x"}, 0)


def test_empty_is_fine_when_not_asserted():
    # nasdaq-halts: a session with no halts is the GOOD outcome.
    assert content_errors({"name": "nasdaq-halts"}, {"title": "NASDAQTrader.com"}, 0) == []


def test_feed_with_no_assertions_is_unchanged():
    assert content_errors({"name": "eia-today"}, {}, 0) == []


# --- staleness (the frozen-WSJ-feed failure mode) ---------------------------

def test_frozen_feed_is_flagged():
    """feeds.a.dj.com/rss/RSSMarketsMain.xml returns perfectly valid RSS
    whose newest item is from January 2025."""
    detail = staleness({"stale_after_hours": 48}, NOW - timedelta(days=570), NOW)
    assert detail and "570" in detail


def test_fresh_feed_is_not_flagged():
    assert staleness({"stale_after_hours": 48}, NOW - timedelta(hours=2), NOW) is None


def test_staleness_is_opt_in():
    assert staleness({}, NOW - timedelta(days=900), NOW) is None


def test_no_parseable_dates_is_not_called_stale():
    # Different fault, different message — don't mislead the dashboard.
    assert staleness({"stale_after_hours": 48}, None, NOW) is None


def test_newest_published_picks_the_max_and_skips_junk():
    entries = [{"published": "Fri, 14 Aug 2026 10:00:00 GMT"},
               {"published": "not a date"},
               {"updated": "2026-08-15T18:00:00Z"},
               {}]
    assert newest_published(entries) == datetime(2026, 8, 15, 18, 0, tzinfo=timezone.utc)


def test_newest_published_empty():
    assert newest_published([]) is None


# --- structured symbols -----------------------------------------------------

def test_symbols_only_from_named_fields():
    feed = {"symbol_fields": ["ndaq_issuesymbol"]}
    assert symbols_from(feed, {"ndaq_issuesymbol": "TALK"}) == ["TALK"]


def test_symbols_absent_by_default():
    # No symbol_fields -> nothing, ever. Inference stays A1's job.
    assert symbols_from({}, {"title": "AAPL soars on results", "ndaq_issuesymbol": "AAPL"}) == []


def test_symbols_split_and_deduped():
    feed = {"symbol_fields": ["syms"]}
    assert symbols_from(feed, {"syms": "aapl, MSFT ,aapl"}) == ["AAPL", "MSFT"]


def test_normalize_rss_carries_symbols_through():
    entry = {"title": "Trading halt: TALK", "id": "nasdaq-halt:TALK:1",
             "published": "Fri, 14 Aug 2026 10:00:00 GMT"}
    item = normalize_rss(entry, feed_name="nasdaq-halts", tier=1, symbols=["talk"])
    assert item.symbols == ["TALK"] and item.source_tier == 1


def test_normalize_rss_without_symbols_is_unchanged():
    entry = {"title": "Acme buys Beta", "id": "g1",
             "published": "Fri, 14 Aug 2026 10:00:00 GMT"}
    assert normalize_rss(entry, feed_name="gnw-manda").symbols == []


# --- nasdaq halts adapter ---------------------------------------------------

# Exactly the shape nasdaqtrader.com serves (probed 2026-08-15), after
# feedparser flattens the ndaq: namespace. Note: no guid, no link.
HALT_ENTRY = {
    "title": "TALK",
    "published": "Fri, 14 Aug 2026 04:00:00 GMT",
    "ndaq_haltdate": "08/14/2026",
    "ndaq_halttime": "19:50:00.000",
    "ndaq_issuesymbol": "TALK",
    "ndaq_issuename": "Talkspace, Inc.  Common Stock",
    "ndaq_market": "NASDAQ",
    "ndaq_reasoncode": "T12",
    "ndaq_resumptiontradetime": "",
}
HALT_FEED = {"name": "nasdaq-halts", "adapter": "nasdaq_halts",
             "symbol_fields": ["ndaq_issuesymbol"]}


def test_raw_halt_item_would_be_quarantined_without_the_adapter():
    """This is WHY the adapter exists: the feed ships no guid and no link."""
    with pytest.raises(NormalizeError) as e:
        normalize_rss(dict(HALT_ENTRY), feed_name="nasdaq-halts")
    assert e.value.reason_code == "MISSING_REQUIRED_FIELD"


def test_adapter_builds_a_readable_headline():
    adapted, syms = adapt_entry(HALT_FEED, dict(HALT_ENTRY), NOW)
    assert syms == ["TALK"]
    assert adapted["title"] == "Trading halt: TALK — Talkspace, Inc. Common Stock [T12]"


def test_adapter_guid_is_stable_and_halt_specific():
    a1, _ = adapt_entry(HALT_FEED, dict(HALT_ENTRY), NOW)
    a2, _ = adapt_entry(HALT_FEED, dict(HALT_ENTRY), NOW + timedelta(minutes=5))
    assert a1["id"] == a2["id"]                     # same halt, same id across polls
    second_halt = dict(HALT_ENTRY, ndaq_halttime="20:10:00.000")
    assert adapt_entry(HALT_FEED, second_halt, NOW)[0]["id"] != a1["id"]


def test_adapter_normalizes_end_to_end():
    adapted, syms = adapt_entry(HALT_FEED, dict(HALT_ENTRY), NOW)
    item = normalize_rss(adapted, feed_name="nasdaq-halts", tier=1,
                         extra_channels=["halt", "market-structure"], symbols=syms)
    assert item.symbols == ["TALK"]
    assert item.source_tier == 1
    assert "halt" in item.channels
    assert item.raw["ndaq_halttime"] == "19:50:00.000"   # kept for later interpretation


def test_backlog_item_is_stamped_at_its_own_et_date_not_now():
    """The startup backlog must never masquerade as fresh: an item from a
    previous ET date gets that date's ET midnight, not the receive time."""
    adapted, _ = adapt_entry(HALT_FEED, dict(HALT_ENTRY), NOW)   # NOW is 08/15
    assert adapted["published"] == datetime(2026, 8, 14, 4, 0, tzinfo=timezone.utc)


def test_same_day_halt_is_stamped_at_receive_time():
    """HaltTime carries no timezone and repeats verbatim across unrelated
    symbols, so we use the receive time (accurate to the 60s poll) rather
    than guessing a zone and being 4-5 hours wrong."""
    same_day = datetime(2026, 8, 14, 23, 55, tzinfo=timezone.utc)
    adapted, _ = adapt_entry(HALT_FEED, dict(HALT_ENTRY), same_day)
    assert adapted["published"] == same_day


def test_adapter_rejects_a_malformed_halt_item():
    with pytest.raises(ValueError):
        adapt_entry(HALT_FEED, {"title": "", "ndaq_haltdate": ""}, NOW)


def test_unknown_adapter_is_loud():
    with pytest.raises(ValueError):
        adapt_entry({"adapter": "does-not-exist"}, {"title": "x"}, NOW)


def test_no_adapter_passes_the_entry_through_untouched():
    entry = {"title": "Acme buys Beta", "id": "g1"}
    adapted, syms = adapt_entry({"name": "gnw-manda"}, dict(entry), NOW)
    assert adapted == entry and syms == []
