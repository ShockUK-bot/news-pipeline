"""v0.19.0: sector data source (SIC ranges, submissions parsing, CIK reverse map,
sector heat) and wiring."""
import json
from pathlib import Path

import yaml

from common.sectors import (SECTORS, load_ticker_ciks, parse_submissions, sector_for_sic,
                            sector_heat_from)

ROOT = Path(__file__).resolve().parents[2]


def test_sic_ranges():
    assert sector_for_sic(3674) == "Information Technology"     # semiconductors (NVDA, MU)
    assert sector_for_sic(7372) == "Information Technology"     # prepackaged software (CRWD, PANW)
    assert sector_for_sic(2834) == "Health Care"                # pharma (BMY, AZN)
    assert sector_for_sic(3841) == "Health Care"
    assert sector_for_sic(6211) == "Financials"                 # brokers (GS)
    assert sector_for_sic(6798) == "Real Estate"                # REITs (SLG)
    assert sector_for_sic(1040) == "Materials"                  # gold mining (NEM)
    assert sector_for_sic(3621) == "Industrials"                # motors and generators (GNRC)
    assert sector_for_sic(3533) == "Energy"                     # oil and gas field machinery (INVX)
    assert sector_for_sic(3663) == "Information Technology"     # comms equipment
    assert sector_for_sic(3634) == "Consumer Discretionary"     # household appliances
    assert sector_for_sic(1311) == "Energy"                     # crude oil and gas
    assert sector_for_sic(2911) == "Energy"
    assert sector_for_sic(4911) == "Utilities"
    assert sector_for_sic(5961) == "Consumer Discretionary"     # catalog and mail order (AMZN)
    assert sector_for_sic(3711) == "Consumer Discretionary"     # motor vehicles (TSLA)
    assert sector_for_sic(3721) == "Industrials"                # aircraft (BA)
    assert sector_for_sic(3812) == "Industrials"                # defense electronics (LMT)
    assert sector_for_sic(2080) == "Consumer Staples"           # beverages (KO)
    assert sector_for_sic(2860) == "Materials"                  # industrial organic chemicals
    assert sector_for_sic(4813) == "Communication Services"     # telephone (VZ)
    assert sector_for_sic(7370) == "Information Technology"
    assert sector_for_sic(None) is None and sector_for_sic("") is None and sector_for_sic(0) is None
    assert sector_for_sic(9999) is None
    assert len(SECTORS) == 11


def test_parse_submissions():
    rec = parse_submissions({"cik": "320193", "sic": "3571", "sicDescription": "Electronic Computers", "name": "Apple Inc."})
    assert rec == {"cik": 320193, "sic": 3571, "sic_description": "Electronic Computers",
                   "sector": "Information Technology", "name": "Apple Inc.",
                   "source": "edgar_submissions"}          # source since v0.21.0
    rec = parse_submissions({"cik": "1", "sic": "", "sicDescription": None, "name": "Shell"})
    assert rec["sic"] is None and rec["sector"] is None


def test_reverse_cik_map(tmp_path):
    f = tmp_path / "cik.json"
    f.write_text(json.dumps({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
                             "1": {"cik_str": 1018724, "ticker": "AMZN", "title": "Amazon"},
                             "2": {"cik_str": 320193, "ticker": "AAPL", "title": "dup"}}))
    m = load_ticker_ciks(str(f))
    assert m == {"AAPL": 320193, "AMZN": 1018724}
    assert load_ticker_ciks(str(tmp_path / "missing.json")) == {}


def test_sector_heat_sum():
    rows = [("Information Technology", 120.0), ("Health Care", 80.0), ("Information Technology", 30.5)]
    assert sector_heat_from(rows, "Information Technology") == 150.5
    assert sector_heat_from(rows, "Energy") == 0.0


def test_wiring():
    a3 = (ROOT / "src" / "a3_risk" / "service.py").read_text()
    assert "sectors.lookup_or_fetch(ticker)" in a3 and "sectors.open_sector_heat(inp.sector)" in a3
    a2 = (ROOT / "src" / "a2_analyst" / "context.py").read_text()
    assert '"sector": await _sector(ticker)' in a2
    wd = yaml.safe_load((ROOT / "config" / "watchdog.yaml").read_text())
    assert "sector-map" in wd["timers"] and wd["heartbeats"]["sectors"]["unit"] == "sector-map"
    assert (ROOT / "schema" / "migrations" / "019-sectors.sql").exists()
    assert "04:40" in (ROOT / "ops" / "systemd" / "sector-map.timer").read_text()
