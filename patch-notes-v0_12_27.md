# v0.12.27 — dashboard: thesis origin chip (2026-08-11)

## The bug (operator-spotted, first morning of the thesis lane)

C6's positions table showed the new v0.12.26 thesis trades as **NEWS**.
The database was right (`positions.origin='thesis'` — C4 stamped it
correctly from the C11 intent body); the front-end was wrong.
`index.html`'s `originChip` was a two-way ternary from the v0.12.0 scanner
release — `scanner → SCAN, everything else → NEWS` — so the new 'thesis'
vocabulary fell through to the NEWS label. Display-only; no journal row,
position, or attribution data was ever wrong.

## The fix

`originChip` becomes an explicit map: `news → NEWS` (blue), `scanner →
SCAN` (amber), `thesis → THESIS` (purple — the analyst-class color, which
is where theses come from). Any FUTURE unknown origin now renders as
itself in grey instead of silently impersonating NEWS — this class of bug
can't recur the next time an origin is added.

Applies everywhere the chip is used: the open-positions table and the
scanner-candidates/history views.

## Files

REPLACED (1): `dashboard/index.html`
NEW (2): `patch-notes-v0_12_27.md`, `v0_12_27-deploy-guide.md`

**3 changed files**, pyproject pencil edit to `0.12.27`. No migration, no
timers, **no restarts** — `dashboard/app.py` reads `index.html` from disk
on every page request, so the fix is live the moment the checkout lands;
just refresh the browser.

## Rollback

`sudo -u trader git -C /opt/pipeline checkout v0.12.26` and refresh. (The
chip goes back to lying about thesis rows; nothing else changes.)
