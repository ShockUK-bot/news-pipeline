# v0.11.4 — Decision tape: show full detail text (no more 70-char cutoff)

Requested directly: "show longer text in the decision tape details section...
I want to understand more what is happening." This patch is the fix.

## Symptom

Every row on the LIVE tab's "Decision tape" panel cuts the reasoning text off
at 70 characters, e.g. `Hyperscaler capex guidance raised; GPU allocation ti`
with the rest silently dropped. Short, sentence-length reasons get cut off
mid-word; anything a model wrote that runs long (A2 thesis reasoning in
particular can run to a few hundred characters) loses most of its content on
the tape, which is exactly the "what is actually happening" context an
operator watching the tape wants.

## Root cause

The truncation is **purely cosmetic, in the browser** — nothing upstream
drops text. Traced it through the whole chain:

- `journal.decisions.reason` (schema/journal-schema.sql) is an unbounded
  `TEXT` column — the model's full reasoning is written there.
- The `dash_decisions` view (`COALESCE(reason, veto_reason) AS detail`) reads
  it back whole, no `LEFT()`/`SUBSTRING()`.
- `dashboard/app.py`'s `/api/state` and the WebSocket push both serialize
  that view straight to JSON — no truncation in Python either.
- `dashboard/index.html`'s `render()` function is where it actually gets cut:
  `<span class="detail">${(d.detail||'').slice(0,70)}</span>` in the decision
  tape row template. The full text arrives in the browser and gets thrown
  away client-side, every render, every row.

So this is a one-file, frontend-only fix.

## Fix

`dashboard/index.html`:

- Dropped the `.slice(0,70)` — the tape now renders `d.detail` in full.
- Changed the tape row from a single flex line to a two-line layout so a
  long reason doesn't blow out the row width or shove the latency (`...ms`)
  column off the edge of the panel: line 1 keeps time / stage chip / ticker
  / action / latency exactly as before; the full detail text now sits on its
  own line below and wraps normally (`.tape-row{flex-wrap:wrap}` + a new
  `.detail.full{flex:1 1 100%;white-space:normal;word-break:break-word;
  overflow-wrap:anywhere}` rule).
- Rows are naturally taller now when a reason is long — the panel's existing
  `#tape{max-height:420px;overflow:auto}` still scrolls the same way, so
  fewer rows are visible at once without scrolling, but nothing is hidden.

**Left alone, on purpose:** the "Thesis" column on the Open positions table
(`.slice(0,80)`) and the "Vetoed trades" panel (`.slice(0,60)`) still
truncate — you only asked about the decision tape. Same one-line change
pattern would apply to those if you want them expanded too later.

## Changed files

- `dashboard/index.html` (CSS: `.tape-row` + new `.detail.full` rule; JS:
  `render()`'s decision-tape row template — dropped the slice, reordered the
  latency span ahead of the now full-width detail span)

## What this does NOT touch

No database, no `app.py`, no schema, no model/agent code, no other service.
`index.html` is read straight off disk on every request to `/`
(c6-dashboard-spec-v1_3.md §9a), so this doesn't even need
`c6-dashboard.service` restarted — the new file takes effect for anyone who
(re)loads the page. If the dashboard is already open in a browser tab, that
tab has the old JS in memory until it's reloaded.

## Tests

None applicable — `tests/integration/test_dashboard.py` exercises the API
layer only (auth, `/api/state` shape, kill/capital flows); it doesn't render
`index.html`, so it has no coverage of this change and none needed updating.

## Rollback

Re-upload the previous `dashboard/index.html` (or `git checkout v0.11.3` on
the Spark) — no other cleanup needed.
