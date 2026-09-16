"""C12 burst stream (v0.15.0) — research-first intraday burst detector.

Subscribes to Alpaca's real-time data websocket (SIP) for a liquid universe,
keeps rolling 5-second buckets per symbol in memory, evaluates burst rules
every few seconds during the session and journals each event with its
forward path into journal.burst_events. NO ORDER PATH: nothing is enqueued,
A3/C4 never see it. Design: docs/claude_1pct-gain-design-2026-09-15.md.
"""
