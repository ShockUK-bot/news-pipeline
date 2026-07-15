"""CIK->ticker mapping (v0.4.6): unit tests for the map itself plus
integration of symbol stamping and skip_unmapped down-routing in the EDGAR
poller. Fixture data mirrors SEC's company_tickers.json format and tonight's
live entities (TransMedics 0001756262)."""
import json
import os
import time

import pytest

os.environ.setdefault("EMBEDDER", "hash")
os.environ.setdefault("QDRANT_PATH", "/tmp/qdrant-cik-test")

from common.clock import utcnow
from c1_ingestion.cik_map import CikMap, _norm_cik
from c1_ingestion.sources.edgar import EdgarSource
from c1_ingestion.store import store_item

pytestmark = pytest.mark.asyncio(loop_scope="session")

SEC_FIXTURE = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1756262, "ticker": "TMDX", "title": "TransMedics Group, Inc."},
    "2": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    "3": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
}


@pytest.fixture()
def map_file(tmp_path):
    p = tmp_path / "cik_map.json"
    p.write_text(json.dumps(SEC_FIXTURE))
    return str(p)


class _FakeMonitor:
    def mark_activity(self):
        pass


def make_source(map_path, skip_unmapped=True):
    os.environ.setdefault("EDGAR_CONTACT", "test@example.com")
    return EdgarSource({"tier": 1, "feed_url": "https://example.invalid/atom",
                        "cik_map_path": map_path,
                        "skip_unmapped": skip_unmapped}, _FakeMonitor())


def edgar_entry(title, acc):
    return {"title": title,
            "id": f"urn:tag:sec.gov,2008:accession-number={acc}",
            "link": f"https://www.sec.gov/x?accession_number={acc}",
            "updated": utcnow().isoformat(),
            "summary": f"<b>AccNo:</b> {acc}"}


# ---- unit: the map ---------------------------------------------------------

def test_norm_cik_handles_padding():
    assert _norm_cik("0001756262") == 1756262
    assert _norm_cik(1756262) == 1756262
    assert _norm_cik("320193") == 320193
    assert _norm_cik("garbage") is None


def test_lookup_and_share_classes(map_file):
    m = CikMap(map_file)
    assert m.known() == 3                       # 4 records, 3 entities
    assert m.tickers_for(["0000320193"]) == ["AAPL"]
    # multi-class: both tickers, file order preserved
    assert m.tickers_for([1652044]) == ["GOOGL", "GOOG"]
    # person CIK (not in registry) contributes nothing; dedup across entities
    assert m.tickers_for(["0002140669", "0001756262", 1756262]) == ["TMDX"]


def test_missing_cache_is_empty_not_fatal(tmp_path):
    m = CikMap(str(tmp_path / "absent.json"))
    assert m.known() == 0
    assert m.tickers_for(["0000320193"]) == []
    assert m.stale()


async def test_refresh_degrades_to_stale_cache(map_file):
    m = CikMap(map_file, refresh_hours=0)       # always stale
    assert m.stale()

    class FailingClient:
        async def get(self, url):
            raise RuntimeError("network down")

    await m.ensure_fresh(FailingClient())
    assert m.known() == 3                       # stale cache still serving


async def test_refresh_replaces_cache(tmp_path):
    p = str(tmp_path / "cik_map.json")
    m = CikMap(p, refresh_hours=0)

    class OkClient:
        class _R:
            text = json.dumps(SEC_FIXTURE)
            def raise_for_status(self):
                pass
        async def get(self, url):
            return self._R()

    await m.ensure_fresh(OkClient())
    assert m.known() == 3
    assert os.path.exists(p)


# ---- integration: stamping + admission in the poller -----------------------

def test_symbols_stamped_from_issuer_cik(map_file):
    """Tonight's live shape: Form 4 with issuer + reporting person. The
    issuer maps; the person doesn't; symbols = the company's ticker."""
    src = make_source(map_file)
    item = src._merge_group([
        edgar_entry("4 - TransMedics Group, Inc. (0001756262) (Issuer)", "0001-26-000001"),
        edgar_entry("4 - Corcoran Nicholas (0001964148) (Reporting)", "0001-26-000001"),
    ])
    assert item.symbols == ["TMDX"]
    assert not src._admit(item)                 # Form 4: whitelist rejects


def test_admit_whitelisted_and_mapped(map_file):
    src = make_source(map_file)
    item = src._merge_group([
        edgar_entry("8-K - Apple Inc. (0000320193) (Filer)", "0002-26-000001")])
    assert item.symbols == ["AAPL"]
    assert src._admit(item)


def test_admit_skips_unmapped_trust(map_file):
    """The asset-backed 8-K flood (GM Financial trusts etc.): whitelisted
    form, but no entity maps to a listed ticker -> archived, not triaged."""
    src = make_source(map_file, skip_unmapped=True)
    item = src._merge_group([
        edgar_entry("8-K - GM Financial Consumer Automobile Receivables "
                    "Trust 2026-3 (0002111111) (Filer)", "0003-26-000001")])
    assert item.symbols == []
    assert not src._admit(item)


def test_admit_unmapped_flows_when_disabled(map_file):
    src = make_source(map_file, skip_unmapped=False)
    item = src._merge_group([
        edgar_entry("8-K - Unknown Newco Inc. (0002999999) (Filer)", "0004-26-000001")])
    assert src._admit(item)                     # A1 inference fallback


def test_failsafe_empty_map_ignores_skip(tmp_path):
    """No cache and fetch failed -> known()==0 -> skip_unmapped must NOT
    black-hole the feed; everything whitelisted flows to A1."""
    src = make_source(str(tmp_path / "absent.json"), skip_unmapped=True)
    item = src._merge_group([
        edgar_entry("8-K - Apple Inc. (0000320193) (Filer)", "0005-26-000001")])
    assert item.symbols == []                   # nothing to map with
    assert src._admit(item)                     # but still admitted


async def test_end_to_end_stored_with_symbols(map_file):
    """Through store_item: the DedupedSignal body carries the stamped
    symbols so A1/A2 receive them without inference."""
    from common.db import get_pool
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("DELETE FROM news.news_items WHERE item_id='edgar:0006-26-000001'")
        await c.execute("DELETE FROM queue.messages WHERE dedup_key LIKE 'edgar:0006-26-000001%'")

    src = make_source(map_file)
    item = src._merge_group([
        edgar_entry("8-K - TransMedics Group, Inc. (0001756262) (Filer)", "0006-26-000001")])
    r = await store_item(item, immutable=True, enqueue=src._admit(item))
    assert r.stored and r.enqueued

    async with pool.connection() as c:
        cur = await c.execute(
            "SELECT symbols FROM news.news_items WHERE item_id='edgar:0006-26-000001'")
        assert (await cur.fetchone())[0] == ["TMDX"]
        cur = await c.execute(
            "SELECT payload->'body'->'symbols' FROM queue.messages "
            "WHERE dedup_key='edgar:0006-26-000001:1'")
        assert (await cur.fetchone())[0] == ["TMDX"]
