"""v0.14.13 — the thesis store gets ticker-bearing input.

1. Router rule 5: a material, ticker-bearing signal at or above
   thesis_copy_min_score ALSO goes to signal.thesis tagged thesis_copy;
   below the score, or with the knob off, routing is unchanged; rule 3
   (ticker-less -> thesis lane) is untouched.
2. Config pins: A5 runs deep every night and is in seeding mode until the
   store holds 6 theses; the router knob is on and set where it admits
   roughly the top 30 signals a day (tier-1 high-urgency band).
"""
from pathlib import Path

import yaml

from a1_triage.schema import TriageOutput
from router.facts import RoutingFacts
from router.rules import (ANALYST_QUEUE, OVERNIGHT_QUEUE, THESIS_QUEUE,
                          THESIS_COPY_ORIGIN, route)

ROOT = Path(__file__).resolve().parents[2] / "config"


def t(tickers=("ACME",), material=True):
    return TriageOutput(material=material, tickers=list(tickers),
                        direction_hint="up", urgency="high",
                        novelty_score=0.9, confidence=0.9, reason="x")


def f(score, market_open=True):
    return RoutingFacts(market_open=market_open, priority_score=score)


def queues(d):
    return [(r.queue, r.origin) for r in d.routes]


# --- rule 5 ---------------------------------------------------------------

def test_high_score_signal_also_feeds_the_thesis_lane():
    d = route(t(), f(16), thesis_copy_min_score=15)
    assert d.action == "ESCALATE"
    assert (ANALYST_QUEUE, None) in queues(d)
    assert (THESIS_QUEUE, THESIS_COPY_ORIGIN) in queues(d)


def test_copy_priority_follows_score():
    d = route(t(), f(16), thesis_copy_min_score=15)
    copy = [r for r in d.routes if r.queue == THESIS_QUEUE][0]
    assert copy.priority == 84                  # 100 - 16: claims first


def test_below_threshold_is_unchanged():
    d = route(t(), f(14), thesis_copy_min_score=15)
    assert queues(d) == [(ANALYST_QUEUE, None)]


def test_knob_off_is_unchanged():
    assert queues(route(t(), f(17))) == [(ANALYST_QUEUE, None)]
    assert queues(route(t(), f(17), thesis_copy_min_score=None)) == \
        [(ANALYST_QUEUE, None)]


def test_overnight_branch_gets_the_copy_too():
    d = route(t(), f(16, market_open=False), thesis_copy_min_score=15)
    assert (OVERNIGHT_QUEUE, None) in queues(d)
    assert (THESIS_QUEUE, THESIS_COPY_ORIGIN) in queues(d)


def test_ticker_less_rule_3_is_untouched():
    d = route(t(tickers=()), f(16), thesis_copy_min_score=15)
    assert queues(d) == [(THESIS_QUEUE, None)]    # one copy, untagged


def test_discard_never_copies():
    d = route(t(material=False), f(16), thesis_copy_min_score=15)
    assert d.action == "DISCARD" and queues(d) == []


# --- config pins ----------------------------------------------------------------

def test_a5_seeds_nightly_until_six_theses():
    a5 = yaml.safe_load((ROOT / "a5.yaml").read_text())
    assert a5["lane"]["force_deep"] is True
    assert a5["store"]["bootstrap_min_theses"] >= 6
    assert 1 <= a5["store"]["bootstrap_target"] <= 6     # NewThesis list max 6
    assert a5["lane"]["wide_max_items"] <= 80          # schema ceiling


def test_router_thesis_copy_is_on_and_selective():
    a1 = yaml.safe_load((ROOT / "a1.yaml").read_text())["router"]
    assert 14 <= int(a1["thesis_copy_min_score"]) <= 17
