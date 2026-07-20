# v0.11.6d — Batch-aware thresholds on the Pipeline load panel

Follow-up to v0.11.6. **One file: `dashboard/index.html`.** Dashboard-only, no
service restart.

## Why

The Pipeline load panel (v0.11.6) coloured every queue with the same
thresholds. But two queues are drained by **daily scheduled jobs**, not
continuous consumers, so a large standing backlog and a multi-hour oldest-wait
are completely normal between runs:

- `signal.overnight` → **A4 pre-market**, runs ~07:00 ET weekdays.
- `signal.thesis` → **A5 thematic**, runs ~21:30 ET daily.

With the old thresholds these two lit up red/amber every single day for no
reason (observed: overnight 41 ready / 10h, thesis 141 / 23h — both perfectly
normal, both flagged). A panel that cries wolf gets ignored, so this fixes it.

## Change

`dashboard/index.html` — the load panel now classifies `signal.overnight` and
`signal.thesis` as **batch** queues:

- **Oldest-wait** goes amber only past **25h** and red past **30h** — i.e. only
  when a daily drain was actually *missed*. Normal same-day accumulation is
  green.
- **Ready count** uses batch thresholds (amber ≥1000, red ≥3000) instead of the
  real-time 50/200.
- Each batch queue is tagged **· scheduled** so it's clear it drains on a timer.
- Real-time queues (`signal.analyst`, `signal.triage`, etc.) keep the tight
  thresholds — a backed-up analyst still flags red immediately.

Result: the two batch queues read green during normal operation and only alarm
if A4 or A5 genuinely didn't run.

## Validation

JavaScript passes `node --check`. Rendered with live-shaped data: overnight
(41/10h) and thesis (141/23h) show green + "· scheduled"; a 412-ready / 11m
analyst still shows red.

## Rollback

`git checkout v0.11.6c` on the Spark, then hard-refresh the dashboard.
