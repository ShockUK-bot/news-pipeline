# v0.12.3 — C6 console tweaks (2026-07-24)

Operator-requested dashboard polish. Two files, no migration, no service
logic touched — safe to deploy any time, including market hours.

## Changes

1. **Decision tape fills its tile.** The tape was hard-capped at 420px, so
   on a tall row it left dead space below and forced scrolling early. The
   tape panel now flexes: the tape takes the tile's full height (matched to
   the right-hand column, floor 340px) and scrolls internally past that —
   measured with an 80-row tape: content fills edge-to-edge, the tile does
   not grow, Pipeline load stays put. — `dashboard/index.html`

2. **CHAT / PERFORMANCE tab exclusivity bug fixed.** The injected CHAT tab
   (app_chat.py, v0.5.2) predates the PERFORMANCE tab and only hid
   LIVE/HISTORY — clicking PERFORMANCE then CHAT left both panels visible
   and both tabs selected. The injection now enumerates every console
   panel/tab generically (any future tab following the id convention is
   handled automatically), and the console's own tab switcher likewise
   hides/deselects the injected chat tab. All 4×4 tab transitions
   verified exclusive in a headless browser against the real app_chat
   entry point. — `dashboard/app_chat.py`, `dashboard/index.html`

3. **Tile repositioning (LIVE tab), per operator:**
   - Momentum scanner now sits directly BELOW Open positions.
   - Vetoed trades and System health swapped — vetoes on top.
   - New order: Open positions → Momentum scanner → Decision tape +
     (Vetoed trades / System health) → Pipeline load.

## Files

REPLACED: `dashboard/index.html`, `dashboard/app_chat.py`
NEW: `patch-notes-v0_12_3.md`, `v0_12_3-deploy-guide.md`

No schema changes, no config changes, no new services.

## Tests

No Python logic changed (app_chat's Python is untouched — only the injected
JS snippet). Verified by rendering the live dashboard headless with seeded
data and screenshot inspection: tape fill metrics (content 3954px inside a
386px tile, internal scroll, layout stable), panel order, and a scripted
walk of every tab transition (initial / perf→chat / chat→perf / chat→live)
asserting exactly one panel visible and one tab selected each time.
Full unit suite unchanged: 329 passed, same single pre-existing failure.

## Rollback

`git checkout v0.12.2` + `sudo systemctl restart c6-dashboard`.
