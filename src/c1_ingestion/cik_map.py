"""CIK -> ticker mapping from SEC's company_tickers.json (v0.4.6).

Models propose, code disposes: an EDGAR filing's affected ticker is
deterministically derivable from its filer CIKs — it was never a judgment
call for A1. This module owns that derivation.

Source: https://www.sec.gov/files/company_tickers.json (~1MB), the SEC's own
CIK->ticker registry, format {"0": {"cik_str": 320193, "ticker": "AAPL",
"title": "Apple Inc."}, ...}. One CIK can map to several tickers (share
classes); we keep them all, primary listing first (SEC orders the file by
market cap, so first occurrence wins for the primary).

Freshness: cached on disk (config: edgar.cik_map_path), refreshed when older
than edgar.cik_map_refresh_hours (default 24). Refresh failures degrade to
the stale cache with a warning — a day-old map is far better than none.
If no cache exists and the first fetch fails, lookups return no tickers and
the poller behaves per edgar.skip_unmapped (fail-safe: with skip_unmapped
false, unmapped filings still reach A1, whose ticker inference remains the
fallback per baseline §4 A1).

SEC fair access applies to this endpoint too: the caller passes its
User-Agent-configured httpx client.
"""
from __future__ import annotations

import json
import os
import time

from common.log import get_logger, kv

log = get_logger("c1.cikmap")

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def _norm_cik(cik: str | int) -> int | None:
    """CIKs arrive zero-padded ('0001756262') or bare (1756262)."""
    try:
        return int(str(cik).strip().lstrip("0") or "0")
    except (ValueError, AttributeError):
        return None


class CikMap:
    def __init__(self, path: str, refresh_hours: float = 24.0):
        self.path = path
        self.refresh_secs = float(refresh_hours) * 3600.0
        self._map: dict[int, list[str]] = {}
        self._loaded_mtime: float | None = None
        if os.path.exists(path):
            self._load_file()

    # ---- lookups ----------------------------------------------------------

    def tickers_for(self, ciks: list[str | int]) -> list[str]:
        """Tickers for a set of entity CIKs, deduped, input order preserved."""
        out: list[str] = []
        for cik in ciks:
            n = _norm_cik(cik)
            if n is None:
                continue
            for t in self._map.get(n, []):
                if t not in out:
                    out.append(t)
        return out

    def known(self) -> int:
        return len(self._map)

    # ---- cache management -------------------------------------------------

    def _load_file(self) -> None:
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("cik map cache unreadable",
                        extra=kv(path=self.path, error=repr(e)[:200]))
            return
        m: dict[int, list[str]] = {}
        for rec in raw.values():
            cik = _norm_cik(rec.get("cik_str", ""))
            ticker = str(rec.get("ticker") or "").strip().upper()
            if cik is None or not ticker:
                continue
            m.setdefault(cik, [])
            if ticker not in m[cik]:
                m[cik].append(ticker)
        self._map = m
        self._loaded_mtime = os.path.getmtime(self.path)
        log.info("cik map loaded", extra=kv(path=self.path, entities=len(m)))

    def stale(self) -> bool:
        if not os.path.exists(self.path):
            return True
        return (time.time() - os.path.getmtime(self.path)) > self.refresh_secs

    async def ensure_fresh(self, client) -> None:
        """Refresh the on-disk cache if stale; always (re)load if the file
        changed. Degrades to the stale cache on any fetch failure."""
        if self.stale():
            try:
                resp = await client.get(SEC_TICKERS_URL)
                resp.raise_for_status()
                json.loads(resp.text)          # validate before replacing
                tmp = self.path + ".tmp"
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(tmp, "w") as f:
                    f.write(resp.text)
                os.replace(tmp, self.path)
                log.info("cik map refreshed", extra=kv(bytes=len(resp.text)))
            except Exception as e:
                log.warning("cik map refresh failed; using stale cache",
                            extra=kv(error=repr(e)[:200],
                                     have_entities=len(self._map)))
        if os.path.exists(self.path):
            mtime = os.path.getmtime(self.path)
            if mtime != self._loaded_mtime:
                self._load_file()
