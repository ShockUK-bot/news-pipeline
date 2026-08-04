"""v0.12.11 unit tests — DB-free: the extended-hours SHADOW lane.

Motivation (operator request, 2026-08-03): most market-moving news drops
outside regular hours; the news lane currently waits for the open and
arrives after the gap. Before building live extended-hours execution
(limit-only orders, no broker stops until 9:30 — the dangerous part), we
MEASURE: during pre/post sessions escalated news is also evaluated at
arrival, quote-based, and journaled WOULD_TRADE / VETO with no order path.

Covered here: the ET session-window arithmetic, the router fan-out (shadow
copy tagged and keyed separately; real overnight path untouched), the
quote-based rule matrix, and the counterfactual next-close walk that lets
post-market rows measure into the next session.
"""
from datetime import datetime, timezone

from common.clock import extended_session
from c3_gate.counterfactual import _next_close_after
from c3_gate.rules import EHState, evaluate_eh
from router.facts import RoutingFacts
from router.rules import route

try:                                    # TriageOutput lives with A1's schema
    from a1_triage.schema import TriageOutput
except Exception:                       # pragma: no cover
    TriageOutput = None


def ET(mo, d, h, m):
    # Aug/summer => ET is UTC-4; build UTC instants for ET wall times.
    from datetime import timedelta
    return (datetime(2026, mo, d, h, m, tzinfo=timezone.utc)
            + timedelta(hours=4))


# ---- extended_session -------------------------------------------------------

def test_pre_session_bounds():
    assert extended_session(ET(8, 4, 3, 59)) is None      # before 4:00 ET
    assert extended_session(ET(8, 4, 4, 0)) == "pre"
    assert extended_session(ET(8, 4, 9, 29)) == "pre"
    assert extended_session(ET(8, 4, 9, 30)) is None      # RTH, not EH


def test_post_session_bounds():
    assert extended_session(ET(8, 4, 15, 59)) is None     # still RTH
    assert extended_session(ET(8, 4, 16, 0)) == "post"
    assert extended_session(ET(8, 4, 19, 59)) == "post"
    assert extended_session(ET(8, 4, 20, 0)) is None      # closed


def test_weekend_is_never_extended():
    assert extended_session(ET(8, 8, 7, 0)) is None       # Saturday
    assert extended_session(ET(8, 9, 17, 0)) is None      # Sunday


# ---- router fan-out ---------------------------------------------------------

def _triage(**over):
    base = dict(material=True, confidence=0.9, tickers=["BA"],
                direction_hint="up", urgency="high", novelty_score=0.8,
                reason="x")
    base.update(over)
    return TriageOutput(**base)


def _routes(facts, eh_shadow=True):
    return route(_triage(), facts, eh_shadow=eh_shadow).routes


def test_eh_window_adds_tagged_analyst_route_and_keeps_overnight():
    facts = RoutingFacts(market_open=False, priority_score=10,
                         eh_session="pre")
    r = _routes(facts)
    queues = [(x.queue, x.origin) for x in r]
    assert ("signal.overnight", None) in queues            # real path untouched
    assert ("signal.analyst", "eh_shadow") in queues       # shadow copy tagged


def test_no_shadow_when_disabled_or_closed_or_open():
    pre = RoutingFacts(market_open=False, priority_score=10, eh_session="pre")
    night = RoutingFacts(market_open=False, priority_score=10, eh_session=None)
    rth = RoutingFacts(market_open=True, priority_score=10, eh_session=None)
    assert all(x.origin is None for x in _routes(pre, eh_shadow=False))
    assert all(x.origin is None for x in _routes(night))
    r = _routes(rth)
    assert [x.queue for x in r] == ["signal.analyst"]      # normal intraday
    assert r[0].origin is None


# ---- evaluate_eh ------------------------------------------------------------

CFG = {"impact_medium_min": 0.02, "impact_high_min": 0.05,
       "extended_pct": 0.06,
       "required_outlets": {"low": {2: 1, 3: 1}, "medium": {2: 1, 3: 2},
                            "high": {2: 2, 3: 3}},
       "eh_shadow": {"enabled": True, "window_min": 30, "max_spread_bps": 100}}

THESIS = {"direction": "up", "magnitude_est": 0.03, "source_risk": "low",
          "ticker": "BA"}


def _state(**over):
    base = dict(prenews_price=100.0, last_price=101.0, bid=100.9, ask=101.1,
                spread_bps=20.0, minutes_since_publish=4, session="pre",
                corroboration_outlets=2, tier_min=2)
    base.update(over)
    return EHState(**base)


def test_fresh_liquid_confirmed_news_would_trade():
    v = evaluate_eh(THESIS, _state(), CFG)
    assert v.verdict == "WOULD_TRADE" and v.veto_reason is None
    assert v.numbers["hypothetical_entry"] == 101.1        # buys the ask
    assert v.numbers["session"] == "pre"


def test_direction_and_credibility_still_apply():
    assert evaluate_eh({**THESIS, "direction": "down"}, _state(),
                       CFG).veto_reason == "LONG_ONLY"
    v = evaluate_eh(THESIS, _state(corroboration_outlets=1, tier_min=3), CFG)
    assert v.veto_reason == "CREDIBILITY"                  # medium/tier3 needs 2


def test_stale_and_already_extended_veto():
    assert evaluate_eh(THESIS, _state(minutes_since_publish=31),
                       CFG).veto_reason == "GATE_WINDOW"
    assert evaluate_eh(THESIS, _state(last_price=107.0),
                       CFG).veto_reason == "GATE_EXTENDED"


def test_liquidity_fails_closed():
    # wide spread, one-sided book, inverted quote, missing quote: all veto —
    # this branch PROPOSES an entry, so missing evidence is a NO.
    assert evaluate_eh(THESIS, _state(spread_bps=180.0),
                       CFG).veto_reason == "EH_LIQUIDITY"
    assert evaluate_eh(THESIS, _state(bid=None),
                       CFG).veto_reason == "EH_LIQUIDITY"
    assert evaluate_eh(THESIS, _state(ask=100.5, bid=100.9),
                       CFG).veto_reason == "EH_LIQUIDITY"
    assert evaluate_eh(THESIS, _state(spread_bps=None),
                       CFG).veto_reason == "EH_LIQUIDITY"


# ---- counterfactual next-close walk ----------------------------------------

def test_post_market_row_measures_into_next_session():
    # veto Tue 2026-08-04 17:00 ET (post) -> next close is Wed 16:00 ET
    close = _next_close_after(ET(8, 4, 17, 0))
    assert close is not None
    assert close.astimezone(timezone.utc) == ET(8, 5, 16, 0)


def test_friday_post_market_walks_over_the_weekend():
    # veto Fri 2026-08-07 17:00 ET -> next close is Monday 08-10 16:00 ET
    close = _next_close_after(ET(8, 7, 17, 0))
    assert close is not None
    assert close.astimezone(timezone.utc) == ET(8, 10, 16, 0)
