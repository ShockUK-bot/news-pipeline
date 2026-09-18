"""v0.22.0: mail kit, evening digest composition, watchdog damping, mailer html, wiring."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from common import mailkit as mk
from c7_watchdog.service import fingerprint, should_alert_damped as should_alert
from evening_digest.service import compose

ROOT = Path(__file__).resolve().parents[2]


def test_mailkit_blocks():
    assert mk.money(792.1) == "+$792" and mk.money(-36.96) == "−$36.96" and mk.money(None) == "—"
    assert mk.money(15000, signed=False) == "$15,000"
    assert mk.colour(1) == mk.GREEN and mk.colour(-1) == mk.RED and mk.colour("x") == mk.INK
    t = mk.table(["A", "B"], [[mk.esc("<x>"), "1"]], align_right={1})   # callers escape (cells may hold chips)
    assert "&lt;x&gt;" in t and "text-align:right" in t
    assert "nothing to show" in mk.table(["A"], [])
    page = mk.page("T", "Head", [mk.tiles([("L", "1", 5)]), mk.needs_you(["do this"])], "sent", "2026-09-17")
    assert "<!doctype html>" in page and "Needs you" in page and "do this" in page and "Head" in page
    assert mk.needs_you([]) == ""


def digest_input(**over):
    base = {"date": "2026-09-17",
            "a7": {"facts": {"trades": {"exits": [
                       {"ticker": "GNRC", "layer": "TARGET", "qty": 34, "price": 210.57, "r_multiple": 1.905,
                        "realized_pnl": 156.06, "is_partial": True, "ts": "2026-09-17T09:02:12-05:00"},
                       {"ticker": "GNRC", "layer": "TRAIL", "qty": 34, "price": 209.35, "r_multiple": 2.41,
                        "realized_pnl": 197.54, "is_partial": False, "ts": "2026-09-17T09:05:20-05:00"}],
                       "opened": [{"ticker": "SMCI", "horizon": "SCALP", "qty_initial": 369, "headline": "up 8% on volume"}],
                       "realized_pnl_today": 353.6},
                   "activity": {"items_ingested": 1200, "vetoes": [{"stage": "GATE", "veto_reason": "CREDIBILITY", "count": 17}]},
                   "guard": {"verdicts": [{"ticker": "GNRC", "recommended_action": "EXIT", "urgency": "high", "thesis_intact": False, "ts": "2026-09-17T09:01:55-05:00"}]},
                   "health_not_ok": [], "ingestion_gaps": []},
                   "narrative": {"summary": "Four trades.", "notables": ["GNRC exit twice"]}},
            "a6": {"reviewed": 3, "recommendations": 1, "stale_flagged": 1,
                   "recos": [{"ticker": "RIOT", "action": "EXIT_RECO", "rationale": "stale"}]},
            "a5": {"summary": "Ten updates.", "new_theses": 0, "status_changes": 0, "evidence_attached": 4, "active_after": 10},
            "c11": {"planned_detail": [], "dead_armed": [{"ticker": "RIOT", "status": "REVIEW_EXIT", "stop": 21.77}], "trim_recos": []},
            "skips": [("AOUT", "ILLIQUID")],
            "positions": [{"ticker": "RIOT", "side": "LONG", "origin": "thesis", "sector": "Financials", "qty": 34,
                           "entry": 21.4259, "last": 21.88, "stop": 21.77, "r_unit": 3.59, "opened": None},
                          {"ticker": "XYZ", "side": "SHORT", "origin": "news", "sector": None, "qty": 10,
                           "entry": 100.0, "last": 90.0, "stop": None, "r_unit": 2.0, "opened": None}],
            "week_realized": 1319.0, "month_realized": 1300.0, "guard_auto": [], "lane_day": [{"origin": "scanner", "n": 2, "pnl": 353.6}],
            "equity": "98553.85"}
    base.update(over)
    return base


def test_compose_digest():
    subject, text, html, needs = compose(digest_input())
    assert subject.startswith("Evening 2026-09-17: up $354, 1 trades, 3 to decide")
    assert needs[0].startswith("RIOT: review exit armed") and any("A6 exit" in n for n in needs) and any("no current stop" in n for n in needs)
    assert "Up $354 today on 1 closed trade, 1 opened. 3 items need you." in html
    assert "Financials" in html and "SHORT" in html and "GNRC" in html and "Four trades." in html
    assert "NEEDS YOU:" in text and "RIOT LONG 34" in text
    # a quiet day
    q = digest_input(a6=None, c11={"planned_detail": [], "dead_armed": [], "trim_recos": []}, skips=[],
                     positions=[], a7={"facts": {"trades": {"exits": [], "opened": [], "realized_pnl_today": 0}}, "narrative": None})
    s2, t2, h2, n2 = compose(q)
    assert n2 == [] and "Flat $0 today on 0 closed trades. Nothing needs you." in h2 and "no exits today" in h2


def test_watchdog_damping():
    now = datetime(2026, 9, 17, 22, 0, tzinfo=timezone.utc)
    warn = [{"severity": "WARNING", "code": "TIMER_STALE", "unit": "a11-metrics.timer", "detail": "x"}]
    crit = [{"severity": "CRITICAL", "code": "SERVICE_DOWN", "unit": "c4-exec", "detail": "x"}]
    # first sighting of a warning: no alert, pending recorded
    mode, pend = should_alert(warn, "", None, now, 6, ("", 0), 2, "")
    assert mode is None and pend == (fingerprint(warn), 1)
    # second consecutive sighting: alert
    mode, pend = should_alert(warn, "", None, now, 6, pend, 2, "")
    assert mode == "NEW" and pend == ("", 0)
    # a critical alerts immediately
    assert should_alert(crit, "", None, now, 6, ("", 0), 2, "")[0] == "NEW"
    # warning-only alert clears silently; critical alert clears with RECOVERED
    assert should_alert([], fingerprint(warn), now, now, 6, ("", 0), 2, "WARNING")[0] == "CLEARED"
    assert should_alert([], fingerprint(crit), now, now, 6, ("", 0), 2, "CRITICAL")[0] == "RECOVERED"
    # repeat after realert window
    assert should_alert(warn, fingerprint(warn), now - timedelta(hours=7), now, 6, ("", 0), 2, "WARNING")[0] == "REPEAT"
    assert should_alert(warn, fingerprint(warn), now - timedelta(hours=1), now, 6, ("", 0), 2, "WARNING")[0] is None


def test_mailer_html_alternative():
    from c5_mailer.service import SmtpTransport
    sent = {}

    class _T(SmtpTransport):
        def __init__(self): super().__init__(env={"MAILER_SMTP_HOST": "h", "MAILER_SMTP_USER": "u", "MAILER_SMTP_PASS": "p", "MAILER_TO": "a@b"})
    import c5_mailer.service as svc
    msgs = []

    class _S:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def send_message(self, m): msgs.append(m)
    svc.smtplib.SMTP_SSL = _S
    _T().send("s", "plain", "<p>rich</p>")
    m = msgs[0]
    assert m.get_content_type() == "multipart/alternative"
    parts = [p.get_content_type() for p in m.iter_parts()]
    assert parts == ["text/plain", "text/html"]


def test_wiring():
    for f in ("a7", "a6", "a5"):
        assert yaml.safe_load((ROOT / "config" / f"{f}.yaml").read_text())
    assert yaml.safe_load((ROOT / "config" / "a7.yaml").read_text())["report"]["email"] is False
    assert yaml.safe_load((ROOT / "config" / "a6.yaml").read_text())["alert"]["email"] is False
    assert yaml.safe_load((ROOT / "config" / "a5.yaml").read_text())["digest"]["email"] is False
    assert yaml.safe_load((ROOT / "config" / "thesis_entry.yaml").read_text())["digest"]["email"] is False
    wd = yaml.safe_load((ROOT / "config" / "watchdog.yaml").read_text())
    assert wd["warn_confirm_passes"] == 2 and "evening-digest" in wd["timers"] and wd["heartbeats"]["digest"]["unit"] == "evening-digest"
    assert "21:20" in (ROOT / "ops" / "systemd" / "evening-digest.timer").read_text()
    assert "html" in (ROOT / "src" / "c5_mailer" / "service.py").read_text()
    assert "render_html" in (ROOT / "src" / "a8_briefing" / "service.py").read_text()
    assert "render_html" in (ROOT / "src" / "a9_review" / "service.py").read_text()


def test_morning_html_on_real_fact_shape():
    from a8_briefing.render import render_html
    facts = {"session_date": "2026-09-17",
             "a4": {"fresh": 182, "ignored": 175, "summary": "Overnight summary.",
                    "open_forwarded": [{"rank": 1, "tickers": ["LGVN"], "headline": "Trading halt: LGVN"}]},
             "thesis": {"active": [{"title": "Biotech Capital Dilution", "driver": "x"}]},
             "positions": [{"side": "LONG", "ticker": "RIOT", "horizon": "LONG_TERM", "qty_open": 34, "avg_entry": 21.43,
                            "last_price": 20.35, "r_progress": -0.15, "position_id": 7, "current_stop": 20.25,
                            "blackout_soon": False, "earnings_next_sessions": 30}],
             "a6": {"review": {"holds": 1, "recos": [{"action": "STALE", "ticker": "RIOT", "rationale": "staleness rule", "position_id": 7}]},
                    "eod": {"verdicts": []}},
             "earnings": {"reporting_today": 42, "held_reporting_soon": []},
             "ops": {"queues": {"signal.thesis": 4}, "outages": [], "health_not_ok": [], "newest_item_age_hours": 0.0}}
    html = render_html(facts, None)
    assert "1 candidate from overnight, 1 open position. 1 item need you." in html or "1 candidate from overnight, 1 open position. 1 items need you." in html
    assert "LGVN" in html and "Biotech Capital Dilution" in html and "STALE" in html and "182 fresh items" in html
    # degenerate shapes must not raise
    render_html({"a4": {"open_forwarded": 7}, "a6": {"review": 3}, "positions": [], "ops": {}, "earnings": {}}, None)
