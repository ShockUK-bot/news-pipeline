-- Paper-soak control initialization (run once, then manage via dashboard/SQL).
-- Schema seeds kill_switch/drawdown_breaker/trading_capital only;
-- max_trades_per_day and block_entries otherwise fall back to code defaults.
INSERT INTO journal.control (key, value, updated_ts) VALUES
  ('trading_capital',    '50000', now()),  -- paper soak capital; min(broker equity, this) drives sizing
  ('max_trades_per_day', '5',     now()),
  ('kill_switch',        '0',     now()),
  ('drawdown_breaker',   '0',     now()),
  ('block_entries',      '0',     now())
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_ts = now();
SELECT key, value, updated_ts FROM journal.control ORDER BY key;
