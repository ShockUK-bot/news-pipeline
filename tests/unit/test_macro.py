"""v0.12.24 — macro series lane: parsers, transforms, feature computation,
and the config pins that keep the lane honest."""
from datetime import date
from pathlib import Path

import pytest
import yaml

from c1_ingestion.macro import (GROUP_ORDER, TRANSFORMS, compute_features,
                                parse_fred_api_json, parse_fredgraph_csv,
                                transform_series, _value_at)
from c1_ingestion.normalize import normalize_rss

CONFIG = Path(__file__).resolve().parents[2] / "config" / "macro.yaml"
SOURCES = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"


# --- fredgraph CSV parsing -------------------------------------------------

def test_parse_fredgraph_classic_header():
    text = "DATE,DGS10\n2026-08-06,4.21\n2026-08-07,4.25\n2026-08-08,.\n"
    rows = parse_fredgraph_csv(text, "DGS10")
    assert rows == [(date(2026, 8, 6), 4.21), (date(2026, 8, 7), 4.25)]


def test_parse_fredgraph_new_header_variant():
    """FRED has shipped both 'DATE' and 'observation_date' headers — the
    parser must accept either without a code change."""
    text = "observation_date,DGS10\n2026-08-07,4.25\n"
    assert parse_fredgraph_csv(text, "DGS10") == [(date(2026, 8, 7), 4.25)]


def test_parse_fredgraph_rejects_html_error_page():
    with pytest.raises(ValueError):
        parse_fredgraph_csv("<html><body>Too Many Requests</body></html>",
                            "DGS10")


def test_parse_fredgraph_skips_junk_rows():
    text = "DATE,X\n2026-08-07,4.25\nnot-a-date,9\n2026-08-08,\n"
    assert parse_fredgraph_csv(text, "X") == [(date(2026, 8, 7), 4.25)]


def test_parse_fred_api_json():
    payload = {"observations": [
        {"date": "2026-08-07", "value": "4.25"},
        {"date": "2026-08-08", "value": "."},
    ]}
    assert parse_fred_api_json(payload, "DGS10") == [(date(2026, 8, 7), 4.25)]
    with pytest.raises(ValueError):
        parse_fred_api_json({"error_message": "bad key"}, "DGS10")


# --- transforms & features -------------------------------------------------

def _monthly(vals, start_year=2025, start_month=1):
    out, y, m = [], start_year, start_month
    for v in vals:
        out.append((date(y, m, 1), float(v)))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def test_value_at_respects_tolerance():
    obs = [(date(2026, 1, 1), 1.0), (date(2026, 6, 1), 2.0)]
    assert _value_at(obs, date(2026, 6, 15)) == 2.0
    # nearest obs is >62 days before the target -> refuse to impersonate it
    assert _value_at(obs, date(2026, 5, 20)) is None


def test_transform_level_and_mom_diff():
    obs = _monthly([100, 103, 101])
    assert transform_series(obs, "level") == obs
    diffs = transform_series(obs, "mom_diff")
    assert [round(v, 6) for _, v in diffs] == [3.0, -2.0]


def test_transform_yoy_pct():
    obs = _monthly([100] * 12 + [104, 105])   # Jan 2026 vs Jan 2025 = +4%
    yoy = transform_series(obs, "yoy_pct")
    assert yoy[0][0] == date(2026, 1, 1)
    assert round(yoy[0][1], 2) == 4.0
    assert round(yoy[1][1], 2) == 5.0


def test_transform_unknown_raises():
    with pytest.raises(ValueError):
        transform_series(_monthly([1, 2]), "median")


def test_compute_features_shape_and_changes():
    obs = _monthly([100, 103, 101, 104, 104.5])   # Jan..May 2025
    f = compute_features(obs, "level")
    assert f["latest"] == 104.5 and f["as_of"] == "2025-05-01"
    assert f["chg_1m"] == 0.5                     # vs Apr
    assert f["chg_3m"] == 4.5                     # 91d back -> Jan 30 -> Jan obs
    assert f["chg_1y"] is None                    # series too short
    assert compute_features([], "level") is None


# --- config pins -----------------------------------------------------------

def test_macro_config_is_coherent():
    cfg = yaml.safe_load(CONFIG.read_text())
    series = cfg["series"]
    assert len(series) >= 10
    ids = [s["id"] for s in series]
    assert len(ids) == len(set(ids)), "duplicate series ids"
    for s in series:
        assert s.get("transform") in TRANSFORMS, s["id"]
        assert s.get("group") in GROUP_ORDER, s["id"]
        assert s.get("label") and s.get("unit") is not None, s["id"]


def test_macro_config_covers_the_core_regime_inputs():
    """The context block is only useful if the big four are all present:
    policy rate, curve, inflation, credit."""
    ids = {s["id"] for s in yaml.safe_load(CONFIG.read_text())["series"]}
    assert {"DFF", "T10Y2Y", "CPIAUCSL", "BAMLH0A0HYM2"} <= ids


def test_macro_feeds_registered_with_tier_and_tags():
    rss = yaml.safe_load(SOURCES.read_text())["rss"]
    feeds = {f["name"]: f for f in rss["feeds"]}
    for name in ("fed-monetary", "bls-releases", "eia-today"):
        assert name in feeds, f"missing macro feed {name}"
        assert "macro" in feeds[name].get("tags", []), name
    assert feeds["fed-monetary"]["tier"] == 1
    assert feeds["bls-releases"]["tier"] == 1


# --- per-feed tier/tags through normalize_rss ------------------------------

ENTRY = {"title": "FOMC statement", "id": "guid-1",
         "published": "2026-08-10T18:00:00Z", "summary": "Rates unchanged.",
         "tags": [{"term": "press-release"}]}


def test_normalize_rss_injects_feed_tags_and_tier():
    item = normalize_rss(dict(ENTRY), feed_name="fed-monetary", tier=1,
                         extra_channels=["macro", "fed"])
    assert item.source_tier == 1
    assert item.channels[:2] == ["macro", "fed"]
    assert "press-release" in item.channels
    assert item.symbols == []                 # ticker-less by nature


def test_normalize_rss_default_unchanged():
    """Back-compat: the wire feeds keep exactly their old behavior."""
    item = normalize_rss(dict(ENTRY), feed_name="prnewswire-news")
    assert item.source_tier == 3
    assert item.channels == ["press-release"]
