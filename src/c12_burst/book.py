"""Per-symbol rolling state built from streamed trades. Pure: no I/O, no clock.

Buckets are BUCKET_SECS wide, keyed by epoch // BUCKET_SECS. The book keeps the
last `keep_secs` of buckets (default 45 min) so a detection at t can be scored
from memory at t+30min without any REST call.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from statistics import median
from typing import Optional

BUCKET_SECS = 5


@dataclass
class Bucket:
    key: int                    # epoch // BUCKET_SECS
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int

    @property
    def ts(self) -> float:
        return self.key * BUCKET_SECS


class SymbolBook:
    def __init__(self, symbol: str, keep_secs: int = 45 * 60):
        self.symbol = symbol
        self.keep = max(1, keep_secs // BUCKET_SECS)
        self.buckets: "OrderedDict[int, Bucket]" = OrderedDict()
        self.last_price: Optional[float] = None
        self.last_ts: Optional[float] = None

    # ---- ingestion ----------------------------------------------------------
    def add_trade(self, ts: float, price: float, size: float) -> None:
        """ts: epoch seconds. Out-of-order trades older than the newest bucket
        are folded into their own bucket if still kept, else dropped."""
        if price <= 0:
            return
        key = int(ts // BUCKET_SECS)
        b = self.buckets.get(key)
        if b is None:
            b = Bucket(key, price, price, price, price, 0.0, 0)
            self.buckets[key] = b
            if len(self.buckets) > self.keep:
                self.buckets.popitem(last=False)
            # keep keys ordered if a late trade created an older bucket
            if len(self.buckets) > 1 and next(reversed(self.buckets)) != key \
                    and key < next(reversed(self.buckets)):
                self.buckets = OrderedDict(sorted(self.buckets.items()))
        b.high = max(b.high, price)
        b.low = min(b.low, price)
        b.close = price
        b.volume += size
        b.trades += 1
        if self.last_ts is None or ts >= self.last_ts:
            self.last_ts, self.last_price = ts, price

    # ---- reads ----------------------------------------------------------------
    def _since(self, now: float, secs: float) -> list[Bucket]:
        lo = int((now - secs) // BUCKET_SECS)
        hi = int(now // BUCKET_SECS)
        return [b for k, b in self.buckets.items() if lo < k <= hi]

    def price_at(self, ts: float) -> Optional[float]:
        """Last traded price at or before ts (bucket close)."""
        key = int(ts // BUCKET_SECS)
        best = None
        for k, b in self.buckets.items():
            if k <= key:
                best = b.close
            else:
                break
        return best

    def ret(self, now: float, secs: float) -> Optional[float]:
        """Return from the price `secs` ago to the latest price."""
        if self.last_price is None:
            return None
        then = self.price_at(now - secs)
        if not then:
            return None
        return self.last_price / then - 1.0

    def vol_mult(self, now: float, window_secs: float, baseline_secs: float,
                 min_baseline_buckets: int = 24,
                 baseline_from_ts: Optional[float] = None) -> Optional[float]:
        """Volume in the last `window_secs` versus the same span of baseline
        (median non-empty bucket volume over `baseline_secs` before the
        window, optionally restricted to buckets at/after baseline_from_ts)."""
        recent = self._since(now, window_secs)
        if not recent:
            return None
        base_lo = now - window_secs - baseline_secs
        base = [b for b in self._since(now - window_secs, baseline_secs)
                if b.volume > 0 and (baseline_from_ts is None or b.ts >= baseline_from_ts)]
        if len(base) < min_baseline_buckets:
            return None
        per_bucket = median(b.volume for b in base)
        if per_bucket <= 0:
            return None
        expected = per_bucket * (window_secs / BUCKET_SECS)
        return sum(b.volume for b in recent) / expected

    def extreme(self, now: float, secs: float, exclude_last: bool = True) -> tuple[Optional[float], Optional[float]]:
        """(high, low) over the last `secs`, excluding the current bucket when
        exclude_last so 'new extreme' compares against the past."""
        bs = self._since(now, secs)
        if exclude_last and bs and bs[-1].key == int(now // BUCKET_SECS):
            bs = bs[:-1]
        if not bs:
            return None, None
        return max(b.high for b in bs), min(b.low for b in bs)

    def entry_after(self, t0: float, delay_secs: float) -> tuple[Optional[float], Optional[float]]:
        """v0.15.4: (price, ts) of the first print at or after t0 + delay_secs
        (bucket open), i.e. what an order sent at detection and working for
        `delay_secs` would realistically get. None if nothing printed yet."""
        key = int((t0 + delay_secs) // BUCKET_SECS)
        for k, b in self.buckets.items():
            if k >= key:
                return b.open, b.ts
        return None, None

    def path(self, t0: float, px: float, direction: int, horizon_secs: int = 1800,
             target: float = 0.01, stop: float = 0.007) -> dict:
        """Forward path from t0 at price px: prices at +60/+300/+900/+1800 s,
        max favourable / adverse excursion, and which of target/stop was hit
        first (both in one bucket counts as stop)."""
        out = {"p_1m": None, "p_5m": None, "p_15m": None, "p_30m": None,
               "max_fav_pct": 0.0, "max_adv_pct": 0.0, "first_hit": "none",
               "first_hit_min": None}
        for name, secs in (("p_1m", 60), ("p_5m", 300), ("p_15m", 900), ("p_30m", 1800)):
            out[name] = self.price_at(t0 + secs)
        key0 = int(t0 // BUCKET_SECS)
        keyN = int((t0 + horizon_secs) // BUCKET_SECS)
        hit = None
        for k, b in self.buckets.items():
            if k <= key0 or k > keyN:
                continue
            fav = (b.high / px - 1) if direction > 0 else (1 - b.low / px)
            adv = (1 - b.low / px) if direction > 0 else (b.high / px - 1)
            out["max_fav_pct"] = max(out["max_fav_pct"], fav)
            out["max_adv_pct"] = max(out["max_adv_pct"], adv)
            if hit is None:
                if adv >= stop:
                    hit = ("stop", (b.ts - t0) / 60)
                elif fav >= target:
                    hit = ("target", (b.ts - t0) / 60)
        if hit:
            out["first_hit"], out["first_hit_min"] = hit[0], round(hit[1], 2)
        return out
