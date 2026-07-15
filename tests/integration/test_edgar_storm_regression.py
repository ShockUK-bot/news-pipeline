"""Regression suite for the 2026-07-14 EDGAR revision-storm incident.

Observed on the Spark: EDGAR's current-events index lists one filing once per
associated entity. The SCHEDULE 13G accession 0002141255-26-000001 appeared
as alternating rows —

    "SCHEDULE 13G - Bakhashwain Mohammed (0002141255) (Filed by)"
    "SCHEDULE 13G - Bitzero Holdings Inc. (0002100457) (Subject)"

— and store_item's latest-hash comparison minted a revision on EVERY poll
cycle (rev 58+ observed on one filing; ~80k items/day; A1 permanently
saturated; GPU pinned at 96%). C2 detected the duplicates (sim>=0.90) and
forwarded them anyway.

These tests replay that exact data and assert the three fixes:
  1. one item per accession, immutable — repeat polls are no-ops
  2. C2 drops duplicate verdicts instead of forwarding
  3. non-whitelisted forms (Form 4, 424B2) are archived but never enqueued
"""
import os

import pytest
import pytest_asyncio

os.environ.setdefault("EMBEDDER", "hash")
os.environ.setdefault("QDRANT_PATH", "/tmp/qdrant-storm-test")

from common.clock import utcnow
from common.db import get_pool
from common.queue import ack, claim
from c1_ingestion.normalize import edgar_accession, edgar_title_parts, normalize_edgar
from c1_ingestion.sources.edgar import (DEFAULT_TRIAGE_FORMS, EdgarSource,
                                        form_whitelisted)
from c1_ingestion.store import store_item
from c2_dedup.cluster import Deduper
from c2_dedup.embedder import get_embedder
from c2_dedup.service import handle_message
from c2_dedup.vectorstore import VectorStore

pytestmark = pytest.mark.asyncio(loop_scope="session")

ACC = "0002141255-26-000001"


def edgar_entry(title: str, acc: str = ACC) -> dict:
    return {
        "title": title,
        "id": f"urn:tag:sec.gov,2008:accession-number={acc}",
        "link": f"https://www.sec.gov/Archives/edgar/data/2100457/{acc.replace('-','')}-index.htm",
        "updated": utcnow().isoformat(),
        "summary": f"<b>Filed:</b> 2026-07-14 <b>AccNo:</b> {acc} <b>Size:</b> 6 KB",
    }


FILED_BY = edgar_entry("SCHEDULE 13G - Bakhashwain Mohammed (0002141255) (Filed by)")
SUBJECT = edgar_entry("SCHEDULE 13G - Bitzero Holdings Inc. (0002100457) (Subject)")


class _FakeMonitor:
    def mark_activity(self):
        pass


def make_source(triage_forms=None) -> EdgarSource:
    os.environ.setdefault("EDGAR_CONTACT", "test@example.com")
    cfg = {"tier": 1, "poll_interval_secs": 15,
           "feed_url": "https://example.invalid/atom"}
    if triage_forms is not None:
        cfg["triage_forms"] = triage_forms
    return EdgarSource(cfg, _FakeMonitor())


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def env():
    import shutil
    shutil.rmtree("/tmp/qdrant-storm-test", ignore_errors=True)
    pool = await get_pool()
    async with pool.connection() as c:
        await c.execute("""
            TRUNCATE news.cluster_members, news.clusters, news.news_items,
                     news.quarantine, news.ingestion_gaps, queue.messages
            RESTART IDENTITY CASCADE""")
    store = VectorStore(path="/tmp/qdrant-storm-test")
    deduper = Deduper(store, get_embedder())
    yield {"pool": pool, "store": store, "deduper": deduper}


# ---------------------------------------------------------------------------
# Unit-level: parsing fixes
# ---------------------------------------------------------------------------

def test_multiword_form_parses():
    """Old regex failed 'SCHEDULE 13G/A' titles entirely (5.5k/day landed in
    the bare {filing} bucket with no form channel)."""
    form, name, cik, role = edgar_title_parts(
        "SCHEDULE 13G/A - Voss Capital, LP (0001730145) (Filed by)")
    assert form == "SCHEDULE 13G/A"
    assert name == "Voss Capital, LP"
    assert cik == "0001730145"
    assert role == "Filed by"


def test_singleword_form_still_parses():
    form, name, cik, role = edgar_title_parts(
        "8-K - CONSTELLATION ENERGY GENERATION LLC (0001168165) (Filer)")
    assert form == "8-K" and cik == "0001168165" and role == "Filer"


def test_accession_extraction():
    assert edgar_accession(FILED_BY) == ACC


def test_form_whitelist_prefix_semantics():
    wl = DEFAULT_TRIAGE_FORMS
    assert form_whitelisted("8-K", wl)
    assert form_whitelisted("8-K/A", wl)           # amendment admitted by prefix
    assert form_whitelisted("SCHEDULE 13G/A", wl)
    assert form_whitelisted("SC 13D/A", wl)
    assert not form_whitelisted("4", wl)           # the 44k/day offender
    assert not form_whitelisted("424B2", wl)       # the 15k/day offender
    assert not form_whitelisted("144", wl)
    assert not form_whitelisted("13F-HR", wl)
    assert not form_whitelisted(None, wl)
    # "4" must not admit "424B2" if someone ever whitelists Form 4
    assert not form_whitelisted("424B2", ["4"])


def test_merge_group_prefers_company_row():
    src = make_source()
    item = src._merge_group([dict(FILED_BY), dict(SUBJECT)])
    # Subject (the company) outranks Filed-by (the person)
    assert item.item_id == f"edgar:{ACC}"
    assert "Bitzero" in item.headline
    ents = item.raw["entities"]
    assert len(ents) == 2
    assert {e["role"] for e in ents} == {"Subject", "Filed by"}
    assert {e["cik"] for e in ents} == {"0002100457", "0002141255"}


# ---------------------------------------------------------------------------
# Integration: the ping-pong replay (fix 1)
# ---------------------------------------------------------------------------

async def test_pingpong_replay_one_item_one_revision(env):
    """The incident, exactly: alternating entity rows across repeated polls.
    Must produce ONE row, revision 1, ONE enqueue — not rev 58."""
    src = make_source()

    # Poll cycle 1: both entity rows arrive together (grouped -> one item)
    item = src._merge_group([dict(FILED_BY), dict(SUBJECT)])
    r1 = await store_item(item, immutable=True, enqueue=True)
    assert r1.stored and r1.revision == 1 and r1.enqueued

    # Poll cycles 2..11: index re-lists the same rows; order flips sometimes
    for i in range(10):
        rows = [dict(SUBJECT), dict(FILED_BY)] if i % 2 else [dict(FILED_BY), dict(SUBJECT)]
        again = src._merge_group(rows)
        r = await store_item(again, immutable=True, enqueue=True)
        assert not r.stored, f"poll {i+2} minted a revision — storm regression"
        assert not r.enqueued

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT count(*), max(revision) FROM news.news_items WHERE item_id=%s",
            (f"edgar:{ACC}",))
        count, max_rev = await cur.fetchone()
        assert count == 1 and max_rev == 1
        cur = await c.execute(
            "SELECT count(*) FROM queue.messages WHERE dedup_key LIKE %s",
            (f"edgar:{ACC}%",))
        assert (await cur.fetchone())[0] == 1


async def test_immutable_beats_even_changed_content(env):
    """Even a genuinely different text under the same accession is a no-op —
    a filing is immutable; changes arrive as new accessions."""
    acc = "0009999999-26-000042"
    e1 = edgar_entry("8-K - ACME CORP (0001234567) (Filer)", acc)
    item1 = normalize_edgar(e1)
    r1 = await store_item(item1, immutable=True)
    assert r1.stored and r1.revision == 1

    e2 = edgar_entry("8-K - ACME CORPORATION LIMITED (0001234567) (Filer)", acc)
    item2 = normalize_edgar(e2)
    assert item2.content_hash != item1.content_hash
    r2 = await store_item(item2, immutable=True)
    assert not r2.stored and r2.revision == 1


async def test_mutable_sources_unaffected(env):
    """Alpaca/RSS revision semantics unchanged: changed hash still revisions."""
    from c1_ingestion.normalize import normalize_alpaca
    pay = {"T": "n", "id": 9901, "headline": "Widget Corp guidance raised",
           "summary": "Q3 outlook up.", "created_at": utcnow().isoformat(),
           "symbols": ["WDGT"], "url": "https://example.com/9901",
           "source": "benzinga"}
    r1 = await store_item(normalize_alpaca(pay))
    assert r1.stored and r1.revision == 1
    pay["headline"] = "Widget Corp guidance raised sharply"
    r2 = await store_item(normalize_alpaca(pay))
    assert r2.stored and r2.revision == 2 and r2.is_correction


# ---------------------------------------------------------------------------
# Integration: form whitelist down-routing (fix 3)
# ---------------------------------------------------------------------------

async def test_form4_archived_but_not_enqueued(env):
    """The 44k/day Form 4 flood: stored as a record, never enters the queue."""
    acc = "0001213900-26-077739"
    entry = edgar_entry("4 - Plum Acquisition Corp, IV (0002030482) (Issuer)", acc)
    src = make_source()
    item = src._merge_group([entry])
    allow = form_whitelisted(item.raw.get("form"), src.triage_forms)
    assert not allow
    r = await store_item(item, immutable=True, enqueue=allow)
    assert r.stored and not r.enqueued

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT count(*) FROM news.news_items WHERE item_id=%s", (f"edgar:{acc}",))
        assert (await cur.fetchone())[0] == 1
        cur = await c.execute(
            "SELECT count(*) FROM queue.messages WHERE dedup_key LIKE %s", (f"edgar:{acc}%",))
        assert (await cur.fetchone())[0] == 0


async def test_8k_whitelisted_and_enqueued(env):
    acc = "0001168165-26-000099"
    entry = edgar_entry("8-K - CONSTELLATION ENERGY GENERATION LLC (0001168165) (Filer)", acc)
    src = make_source()
    item = src._merge_group([entry])
    allow = form_whitelisted(item.raw.get("form"), src.triage_forms)
    assert allow
    r = await store_item(item, immutable=True, enqueue=allow)
    assert r.stored and r.enqueued
    assert "8-K" in item.channels          # router convenience channel intact


# ---------------------------------------------------------------------------
# Integration: C2 drops duplicates (fix 2)
# ---------------------------------------------------------------------------

async def _run_c2_once(env):
    msg = await claim("signal.dedup", "test-c2-storm")
    assert msg is not None
    await handle_message(msg, env["deduper"])
    await ack(msg.msg_id)
    return msg


async def test_c2_drops_duplicate_forwards_original(env):
    """Two distinct items with near-identical text (two 13G rows that slipped
    grouping, or the same story twice): first forwards, duplicate does not."""
    from c1_ingestion.normalize import normalize_rss
    text = "Bitzero Holdings SCHEDULE 13G beneficial ownership disclosure filed today"
    a = normalize_rss({"title": text, "id": "https://w.example/a",
                       "link": "https://w.example/a",
                       "published": utcnow().isoformat(),
                       "summary": "Ownership stake disclosed."}, feed_name="w1")
    b = normalize_rss({"title": text, "id": "https://w.example/b",
                       "link": "https://w.example/b",
                       "published": utcnow().isoformat(),
                       "summary": "Ownership stake disclosed."}, feed_name="w2")
    assert a.item_id != b.item_id           # distinct items, identical text

    # Drain messages left on signal.dedup by earlier tests — claim() is FIFO
    # and would hand us those instead of ours (the known fixture-poisoning
    # pattern from the Phase 4 suite).
    while True:
        stale = await claim("signal.dedup", "test-c2-storm-drain")
        if stale is None:
            break
        await ack(stale.msg_id)

    await store_item(a)
    await store_item(b)
    await _run_c2_once(env)                 # a -> new story, forwards
    await _run_c2_once(env)                 # b -> duplicate, drops

    async with env["pool"].connection() as c:
        cur = await c.execute(
            "SELECT count(*) FROM queue.messages WHERE queue_name='signal.triage' "
            "AND dedup_key LIKE 'rss:%'")
        n_triage = (await cur.fetchone())[0]
        assert n_triage == 1, f"duplicate reached triage (got {n_triage})"
        # corroboration still recorded for C3's credibility input
        cur = await c.execute(
            """SELECT cm.cluster_id, count(*) FROM news.cluster_members cm
               JOIN news.news_items ni ON ni.item_id = cm.item_id
               WHERE ni.source LIKE 'rss:%' GROUP BY 1""")
        rows = await cur.fetchall()
        assert rows and rows[0][1] == 2, "duplicate membership not recorded"
