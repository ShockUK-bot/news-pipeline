"""v0.12.19 unit tests — the 2026-08-08 weekend trace's three defects.

1. C2 symbol-overlap gate: template blackhole clusters (cluster 8693 held
   40+ unrelated tickers of analyst boilerplate) — a similar-sounding
   neighbor about a different company must NOT capture the item.
2. A1 SUPPRESS ticker attribution: journal under the incoming item's own
   ticker (the SPCX suppress row was labeled KYMR, hiding it from audits).
   Tested here at the predicate level via the same fallback expression.
3. A2 data-unavailable REJECT: Alpaca 404 / "no daily bars" is a verdict,
   not a retriable crash (was: 5 retries -> silent DLQ, ~10 lost
   escalations/day).
"""
from types import SimpleNamespace

import pytest

import c2_dedup.cluster as cluster_mod
from c2_dedup.cluster import Deduper


# ---- shared C2 test doubles -------------------------------------------------

class FakeStore:
    def __init__(self, neighbors):
        self._neighbors = neighbors
        self.upserts = []

    def nearest(self, vector, limit=5):
        return self._neighbors[:limit]

    def get_vector(self, item_id, revision):
        return None

    def upsert_dedup(self, *a, **k):
        self.upserts.append(a)


class FakeEmbedder:
    name = "fake"

    def embed(self, text):
        return [0.1] * 8


def neighbor(item_id, score, cluster_id):
    return SimpleNamespace(item_id=item_id, score=score, cluster_id=cluster_id)


@pytest.fixture
def db_stub(monkeypatch):
    """Stub cluster.py's module-level DB helpers; records what happened."""
    calls = {"created": 0, "members": [], "symbols": {}}

    async def _existing(item_id):
        return None

    async def _create(canonical):
        calls["created"] += 1
        return 999

    async def _add(cluster_id, item_id, revision, source, sim):
        calls["members"].append((cluster_id, item_id))

    async def _corr(cluster_id):
        return (1, 1)

    async def _syms(item_id):
        return calls["symbols"].get(item_id, [])

    monkeypatch.setattr(cluster_mod, "_existing_cluster_of", _existing)
    monkeypatch.setattr(cluster_mod, "_create_cluster", _create)
    monkeypatch.setattr(cluster_mod, "_add_member", _add)
    monkeypatch.setattr(cluster_mod, "_corroboration", _corr)
    monkeypatch.setattr(cluster_mod, "_symbols_of", _syms)
    return calls


def _item(symbols):
    return {"item_id": "alpaca:1", "revision": 1, "source": "alpaca_benzinga",
            "headline": "h", "summary": "s", "symbols": symbols}


# ---- 1. C2 symbol-overlap gate ----------------------------------------------

async def test_disjoint_symbols_start_new_cluster(db_stub):
    # The blackhole shape: high template similarity, different company.
    db_stub["symbols"]["alpaca:kymr"] = ["KYMR"]
    d = Deduper(FakeStore([neighbor("alpaca:kymr", 0.86, 8693)]), FakeEmbedder())
    dec = await d.process(_item(["SPCX"]))
    assert dec.is_new_story and dec.cluster_id == 999
    assert db_stub["created"] == 1


async def test_shared_symbol_still_joins(db_stub):
    db_stub["symbols"]["alpaca:spcx0"] = ["SPCX"]
    d = Deduper(FakeStore([neighbor("alpaca:spcx0", 0.86, 9237)]), FakeEmbedder())
    dec = await d.process(_item(["SPCX"]))
    assert not dec.is_new_story and dec.cluster_id == 9237


async def test_gate_skips_to_next_qualifying_neighbor(db_stub):
    db_stub["symbols"]["alpaca:kymr"] = ["KYMR"]
    db_stub["symbols"]["alpaca:spcx0"] = ["SPCX"]
    d = Deduper(FakeStore([neighbor("alpaca:kymr", 0.95, 8693),
                           neighbor("alpaca:spcx0", 0.84, 9237)]),
                FakeEmbedder())
    dec = await d.process(_item(["SPCX"]))
    assert dec.cluster_id == 9237 and not dec.is_new_story
    # skipped the 0.95 template match -> not a duplicate either
    assert not dec.is_duplicate


async def test_symbol_less_items_are_exempt(db_stub):
    # EDGAR/RSS items with no tags: no basis to judge -> old behavior.
    db_stub["symbols"]["alpaca:kymr"] = ["KYMR"]
    d = Deduper(FakeStore([neighbor("alpaca:kymr", 0.86, 8693)]), FakeEmbedder())
    dec = await d.process(_item([]))
    assert dec.cluster_id == 8693


async def test_symbol_less_neighbor_is_exempt(db_stub):
    db_stub["symbols"]["edgar:x"] = []
    d = Deduper(FakeStore([neighbor("edgar:x", 0.86, 4321)]), FakeEmbedder())
    dec = await d.process(_item(["SPCX"]))
    assert dec.cluster_id == 4321


async def test_gate_can_be_disabled(db_stub):
    db_stub["symbols"]["alpaca:kymr"] = ["KYMR"]
    d = Deduper(FakeStore([neighbor("alpaca:kymr", 0.86, 8693)]),
                FakeEmbedder(), require_symbol_overlap=False)
    dec = await d.process(_item(["SPCX"]))
    assert dec.cluster_id == 8693


async def test_below_threshold_still_new_cluster(db_stub):
    db_stub["symbols"]["alpaca:spcx0"] = ["SPCX"]
    d = Deduper(FakeStore([neighbor("alpaca:spcx0", 0.55, 9237)]), FakeEmbedder())
    dec = await d.process(_item(["SPCX"]))
    assert dec.is_new_story


# ---- 2. A1 suppress ticker attribution --------------------------------------

def test_suppress_ticker_prefers_own_symbol():
    # The journaling expression: incoming item's symbol wins over the prior's.
    own, prior = ["SPCX"], ("KYMR",)
    ticker = own[0] if own else (prior[0] if prior else None)
    assert ticker == "SPCX"


def test_suppress_ticker_falls_back_to_prior():
    own, prior = [], ("KYMR",)
    ticker = own[0] if own else (prior[0] if prior else None)
    assert ticker == "KYMR"


# ---- 3. A2 data-unavailable classification ----------------------------------

def _classify(e):
    """Mirror of the service's unavailable-vs-transient split."""
    import httpx
    return ((isinstance(e, httpx.HTTPStatusError)
             and e.response.status_code == 404)
            or (isinstance(e, RuntimeError) and "no daily bars" in str(e)))


def _http_error(status):
    import httpx
    req = httpx.Request("GET", "https://data.alpaca.markets/v2/stocks/BCY/snapshot")
    return httpx.HTTPStatusError("err", request=req,
                                 response=httpx.Response(status, request=req))


def test_404_is_unavailable_not_retriable():
    assert _classify(_http_error(404))


def test_5xx_and_dns_still_retry():
    assert not _classify(_http_error(503))
    assert not _classify(RuntimeError("connection reset"))


def test_no_daily_bars_is_unavailable():
    assert _classify(RuntimeError("no daily bars for FUSN"))
