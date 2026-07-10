"""A2 context pack — everything the analyst sees beyond the item itself,
assembled by code (baseline: "answered against actual price action provided
in context", never model memory).

Included in Phase 3:
  price_action      pre-news reference, last, % move since received_ts,
                    volume multiple vs 20d average minute volume
  daily_context     prev close, ATR(14), ADV(20)
  related_headlines top-k from the retrieval collection (material items only)
  regime            latest C8 snapshot features
Deferred (P1 sources not yet integrated; keys present, value null, so the
prompt shape is stable): sector, earnings_date, short_interest, thesis_matches.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from common.clock import parse_ts, utcnow
from common.db import get_pool
from common.log import get_logger
from common.marketdata import MarketData, adv20, atr14, avg_minute_volume
from c2_dedup.embedder import embed_text_for
from c2_dedup.vectorstore import VectorStore

log = get_logger("a2.context")


async def _regime_features() -> tuple[int | None, dict | None]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT regime_id, features FROM journal.regime_snapshots
               ORDER BY ts DESC LIMIT 1""")
        row = await cur.fetchone()
        return (row[0], row[1]) if row else (None, None)


async def build_context(md: MarketData, store: VectorStore, embedder,
                        item: dict, ticker: str) -> tuple[dict, int | None]:
    """Returns (context dict for the prompt, regime_id for the decision row)."""
    received = parse_ts(item.get("received_ts") or item["published_ts"])
    now = utcnow()

    daily = await md.daily_bars(ticker, 30)
    quote = await md.snapshot(ticker)
    prev = await md.prev_close(ticker)

    # pre-news reference: last minute close before received_ts, else prev close
    pre_start = received - timedelta(minutes=30)
    pre_bars = await md.minute_bars(ticker, pre_start, received)
    prenews_price = pre_bars[-1]["close"] if pre_bars else prev

    since_bars = await md.minute_bars(ticker, received, now)
    baseline_bars = await md.minute_bars(ticker, received - timedelta(days=5), received)
    base_vol = avg_minute_volume(baseline_bars)
    since_vol = avg_minute_volume(since_bars)

    pct_move = round((quote.price - prenews_price) / prenews_price, 5) if prenews_price else None
    vol_mult = round(since_vol / base_vol, 2) if (since_vol and base_vol) else None

    related = []
    if item.get("headline"):
        vec = embedder.embed(embed_text_for(item["headline"], item.get("summary")))
        for hit in store.related(vec, limit=6):
            if hit.get("item_id") != item.get("item_id"):
                related.append({"headline": hit.get("headline"),
                                "tickers": hit.get("tickers"),
                                "published_ts": hit.get("published_ts"),
                                "similarity": round(hit.get("score", 0.0), 3)})

    regime_id, regime = await _regime_features()

    context = {
        "price_action": {
            "prenews_price": prenews_price,
            "last": quote.price,
            "pct_move_since_news": pct_move,
            "volume_multiple": vol_mult,
            "minutes_since_news": int((now - received).total_seconds() // 60),
        },
        "daily_context": {
            "prev_close": prev,
            "atr_14": atr14(daily),
            "adv_20d": adv20(daily),
        },
        "related_headlines": related[:5],
        "regime": regime,
        # P1 sources, deferred — stable keys, null values:
        "sector": None,
        "earnings_date": None,
        "short_interest": None,
        "thesis_matches": [],
    }
    return context, regime_id

