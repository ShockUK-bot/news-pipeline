"""v0.20.0: sector policy (3% net, scanner exempt), cluster rule, dashboard sector views."""
import sys
from pathlib import Path

import yaml

from a3_risk.sizing import cluster_verdict, scanner_capital_cfg, size_entry
from common.sectors import sector_heat_breakdown

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_risk_exec import CAPITAL, LIMITS, PROFILE, inputs  # noqa: E402


def test_sector_clip_binds_only_when_cap_set():
    # 50k capital, 1.5% cap = 750 risk; sector already holds 700 -> 50 left -> 25 shares
    cap = dict(CAPITAL)
    clipped = size_entry(inputs(atr_14=1.0, sector="Information Technology", sector_heat=700.0),
                         cap, LIMITS, PROFILE, "LONG", 2.0)
    assert clipped.verdict == "VETO" and clipped.veto_reason == "SIZE_CLIPPED"
    assert clipped.numbers["sector"] == "Information Technology" and clipped.numbers["sector_heat"] == 700.0
    # the scanner lane switches the clip off: same inputs size normally
    lane = scanner_capital_cfg(CAPITAL, {"risk_multiplier": 1.0, "max_position_notional_pct": 0.30, "sector_clip": False})
    assert lane["max_sector_heat_pct"] is None
    free = size_entry(inputs(atr_14=1.0, sector="Information Technology", sector_heat=700.0),
                      lane, LIMITS, PROFILE, "LONG", 2.0)
    assert free.verdict == "SIZE" and "sector_heat" not in free.numbers["clips"]
    # unknown sector still flags, never clips
    unk = size_entry(inputs(atr_14=1.0), cap, LIMITS, PROFILE, "LONG", 2.0)
    assert "SECTOR_UNKNOWN" in unk.flags and "sector_heat" not in unk.numbers["clips"]


def test_net_heat_offsets_a_pair():
    rows = [("Information Technology", 300.0, "LONG"), ("Information Technology", 250.0, "SHORT"),
            ("Health Care", 100.0, "LONG")]
    b = sector_heat_breakdown(rows, "Information Technology")
    assert b == {"gross": 550.0, "net": 50.0, "long": 300.0, "short": 250.0, "n": 2}
    assert sector_heat_breakdown(rows, "Energy")["n"] == 0


def test_cluster_verdict_modes():
    assert cluster_verdict(0, {"mode": "shadow", "max_per_sector": 1}) == (False, "shadow")
    assert cluster_verdict(1, {"mode": "shadow", "max_per_sector": 1}) == (True, "shadow")
    assert cluster_verdict(1, {"mode": "veto", "max_per_sector": 1}) == (True, "veto")
    assert cluster_verdict(5, {"mode": "off"}) == (False, "off")
    assert cluster_verdict(1, None) == (True, "shadow")


def test_risk_yaml_pins():
    r = yaml.safe_load((ROOT / "config" / "risk.yaml").read_text())
    assert r["capital"]["max_sector_heat_pct"] == 0.03
    assert r["capital"]["sector_heat_mode"] == "net"
    assert r["scanner"]["sector_clip"] is False
    assert r["scanner"]["sector_cluster"]["mode"] == "shadow" and r["scanner"]["sector_cluster"]["max_per_sector"] == 1


def test_dashboard_shapes():
    app = (ROOT / "dashboard" / "app.py").read_text()
    html = (ROOT / "dashboard" / "index.html").read_text()
    assert "LEFT JOIN journal.sectors s USING (ticker)" in app and '"sectors": sectors' in app
    assert "<th>Sector</th>" in html and 'id="sectors"' in html and "sectorPolicy" in html
    a3 = (ROOT / "src" / "a3_risk" / "service.py").read_text()
    assert "SCANNER_SECTOR_CLUSTER" in a3 and "sector_heat_breakdown" in a3
