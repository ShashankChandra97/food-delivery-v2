PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  region TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  order_id TEXT,
  event_type TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  response TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  event_id TEXT,
  order_id TEXT,
  event_type TEXT NOT NULL,
  region TEXT NOT NULL,
  created_at REAL NOT NULL,
  status TEXT NOT NULL,
  http_status INTEGER NOT NULL,
  failure INTEGER NOT NULL,
  escalation INTEGER NOT NULL,
  duplicate INTEGER NOT NULL DEFAULT 0,
  duration_ms REAL NOT NULL,
  execution_id TEXT,
  details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_time ON runs(created_at);
CREATE TABLE IF NOT EXISTS api_calls (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  operation TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  success INTEGER NOT NULL,
  duration_ms REAL NOT NULL,
  error TEXT,
  observed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY,
  dedupe_key TEXT UNIQUE NOT NULL,
  created_at REAL NOT NULL,
  order_id TEXT,
  recipient TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  delivery_status TEXT NOT NULL DEFAULT 'LOCAL_INBOX'
);
CREATE TABLE IF NOT EXISTS alerts (
  alert_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  region TEXT NOT NULL,
  opened_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  resolved_at REAL,
  details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS region_switches (
  region TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL,
  reason TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS loki_outbox (
  run_id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  payload TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  sent_at REAL,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE VIEW IF NOT EXISTS daily_metrics AS
SELECT date(created_at,'unixepoch') AS day,
  count(*) AS runs, sum(failure) AS failures, sum(escalation) AS escalations,
  1.0*sum(failure)/count(*) AS failure_rate,
  1.0*sum(escalation)/count(*) AS escalation_rate
FROM runs WHERE duplicate=0 AND event_type NOT IN ('maintenance','workflow_error','region_toggle')
GROUP BY day;
