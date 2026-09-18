# Patch notes v0.22.0 (2026-09-17, 22:00 CT): two emails a day, in HTML

Operator direction: too many plain text emails (about 12 a day this week, 9 of them watchdog), hard to tell what each one says. This release cuts them to two on a normal day and renders every one from a shared template.

## What you receive now

- **06:35 CT, morning briefing** (A8): unchanged content, now HTML. Headline that says what matters, tiles (open positions, unrealised, candidates, recommendations, earnings today), a "needs you" box, open positions with stop and R, overnight candidates, the narrative, pipeline state.
- **21:20 CT, evening digest** (new `evening-digest` timer): one email replacing the end of day report (15:37), the A6 position review (19:01), the A5 thesis digest (20:35) and the C11 entry plan (21:15). Composed from what those four journaled today, no model call. Headline ("Up 792 today on 3 closed trades, 4 opened. 2 items need you."), tiles (realised today, week, month, open positions, unrealised, equity), "needs you" (exits armed for the open, A6 exit and trim recommendations, guard auto exits, health problems, positions without a stop), trades today with lane split, opened today, open positions with sector, tomorrow's thesis plan and skips, thesis store summary, guard verdicts, vetoes, ingestion gaps, and A7's operator's log. Skipped with a journal row when A7 did not run (holiday).
- **Saturday 09:00 CT, A9 review**: HTML cards per proposal with the evidence table and the approve wording; lane tiles; evaluations; watch list; weekly rollups.
- **Alerts** only when something is wrong and still wrong: a warning only finding set must be seen on two consecutive watchdog passes (10 minutes) before it emails; a warning that clears sends nothing; criticals email at once and send one recovery email. HTML.

## Files

`src/common/mailkit.py` (page, tiles, table, section, needs you, chips, money and percent formatting; pure), `src/evening_digest/service.py`, `ops/systemd/evening-digest.service` and `.timer`, `src/c5_mailer/service.py` (multipart alternative when `html` is present), `schema/migrations/020-outbox-html.sql` (`journal.outbox.html`, kind `EVENING_DIGEST`; applied 21:52 CT), `src/a8_briefing/render.py` `render_html` and its outbox insert, `src/a9_review/service.py` `render_html`, `src/c7_watchdog/service.py` (`should_alert_damped`, `render_html`, control keys `watchdog_pending_fp`, `watchdog_pending_n`, `watchdog_last_alert_sev`), `src/a7_report/service.py` (`report.email` gate). Config: `a7.yaml report.email: false`, `a6.yaml alert.email: false`, `a5.yaml digest.email: false`, `thesis_entry.yaml digest.email: false`, `watchdog.yaml warn_confirm_passes: 2` plus the timer and `digest` heartbeat. `CLAUDE.md` services and email notes.

## Tests, verification, services

`tests/unit/test_v0_22_0.py` (mail kit, digest composition on a busy and a quiet day, watchdog damping lifecycle, mailer multipart, wiring). Suite 890 passed. Digest dry run on today's journal, then sent for real at 21:55 CT as the first sample (outbox 210). No long running service touched: every producer is a timer and picks the change up on its next run.

## Rollback

Set the four `email` flags back to true, `report.email: true`, remove `warn_confirm_passes`, disable `evening-digest.timer`. Plain text stays as the fallback in every message, so nothing depends on the HTML.
