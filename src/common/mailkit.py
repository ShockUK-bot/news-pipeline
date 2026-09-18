"""Shared HTML email kit (v0.22.0). Every pipeline email is built from the
same few blocks so they read as one product: a headline that says the one
thing that matters, a row of tiles, sections with tables, a "needs you" box
when a decision is waiting. Inline styles only (mail clients strip <style>);
tables for layout; no images. Pure functions, tested."""
from __future__ import annotations

import html as _html
from typing import Iterable, Optional

GREEN, RED, INK, MUTED, LINE, BG, CARD, ACCENT = ("#1a7f4b", "#b3261e", "#1b1b1f", "#6b6f76",
                                                    "#e4e6ea", "#f4f5f7", "#ffffff", "#2b4c8c")


def esc(x) -> str:
    return _html.escape("" if x is None else str(x))


def money(x, signed: bool = True) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    s = "0" if v == 0 else f"{abs(v):,.0f}" if abs(v) >= 100 else f"{abs(v):,.2f}"
    return (("+" if v > 0 else "−" if v < 0 else "") if signed else ("−" if v < 0 else "")) + "$" + s


def pct(x, digits: int = 2) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{'+' if v > 0 else ''}{v:.{digits}f}%"


def num(x, digits: int = 2) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.{digits}f}"


def colour(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return INK
    return GREEN if f > 0 else RED if f < 0 else INK


def chip(text: str, bg: str = "#e8edf6", fg: str = ACCENT) -> str:
    return (f'<span style="display:inline-block;padding:1px 7px;border-radius:10px;'
            f'font-size:11px;font-weight:600;background:{bg};color:{fg}">{esc(text)}</span>')


def side_chip(side: str) -> str:
    return chip("SHORT", "#fbe9e7", RED) if str(side).upper() == "SHORT" else chip("LONG", "#e6f4ec", GREEN)


def tiles(items: Iterable[tuple]) -> str:
    """items: (label, value_html, tone) where tone is a number for colour, None for neutral."""
    cells = []
    for label, value, tone in items:
        col = colour(tone) if tone is not None else INK
        cells.append(
            f'<td style="padding:6px 6px 6px 0;vertical-align:top">'
            f'<div style="background:{CARD};border:1px solid {LINE};border-radius:8px;padding:10px 12px;min-width:110px">'
            f'<div style="font-size:11px;color:{MUTED};text-transform:uppercase;letter-spacing:.04em">{esc(label)}</div>'
            f'<div style="font-size:20px;font-weight:700;color:{col};margin-top:2px">{value}</div></div></td>')
    return f'<table role="presentation" cellspacing="0" cellpadding="0" style="border-collapse:collapse"><tr>{"".join(cells)}</tr></table>'


def table(cols: list[str], rows: list[list[str]], empty: str = "nothing to show",
          align_right: Optional[set] = None) -> str:
    align_right = align_right or set()
    if not rows:
        return f'<div style="color:{MUTED};font-size:13px;padding:6px 0">{esc(empty)}</div>'
    th = "".join(f'<th style="text-align:{"right" if i in align_right else "left"};font-size:11px;color:{MUTED};'
                 f'font-weight:600;padding:6px 8px;border-bottom:1px solid {LINE};text-transform:uppercase;letter-spacing:.03em">{esc(c)}</th>'
                 for i, c in enumerate(cols))
    body = []
    for r in rows:
        tds = "".join(f'<td style="text-align:{"right" if i in align_right else "left"};font-size:13px;padding:6px 8px;'
                      f'border-bottom:1px solid {LINE};white-space:nowrap">{c}</td>' for i, c in enumerate(r))
        body.append(f"<tr>{tds}</tr>")
    return (f'<table role="presentation" cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%">'
            f'<thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table>')


def section(title: str, inner: str, note: Optional[str] = None) -> str:
    n = f'<div style="font-size:12px;color:{MUTED};margin:2px 0 6px">{esc(note)}</div>' if note else ""
    return (f'<div style="background:{CARD};border:1px solid {LINE};border-radius:10px;padding:14px 16px;margin:12px 0">'
            f'<div style="font-size:14px;font-weight:700;color:{INK}">{esc(title)}</div>{n}{inner}</div>')


def bullets(items: Iterable[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    return "<ul style='margin:6px 0 0 18px;padding:0;font-size:13px;line-height:1.5'>" + \
           "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>"


def needs_you(items: list[str]) -> str:
    if not items:
        return ""
    return (f'<div style="background:#fff6e5;border:1px solid #f0c36d;border-radius:10px;padding:12px 16px;margin:12px 0">'
            f'<div style="font-size:13px;font-weight:700;color:#7a4b00">Needs you</div>'
            + bullets(items) + "</div>")


def page(title: str, headline: str, blocks: list[str], sent_by: str, when: str) -> str:
    return (f'<!doctype html><html><body style="margin:0;padding:0;background:{BG}">'
            f'<div style="max-width:720px;margin:0 auto;padding:18px 14px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:{INK}">'
            f'<div style="font-size:12px;color:{MUTED}">{esc(title)} · {esc(when)}</div>'
            f'<div style="font-size:20px;font-weight:700;line-height:1.3;margin:4px 0 12px">{esc(headline)}</div>'
            + "".join(blocks) +
            f'<div style="font-size:11px;color:{MUTED};margin-top:16px">{esc(sent_by)}</div>'
            f'</div></body></html>')
