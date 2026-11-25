CREATE TABLE test_workers (
  worker_id TEXT PRIMARY KEY,
  zone TEXT NOT NULL CHECK (zone IN ('blue', 'yellow')),
  repo TEXT NOT NULL,
  pr_number INTEGER,
  pr_head_sha TEXT,
  run_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('DEPLOYING', 'ACTIVE', 'STOPPED')),
  ttl_seconds INTEGER NOT NULL DEFAULT 14400,
  expires_at INTEGER NOT NULL,
  registered_at INTEGER NOT NULL,
  deregistered_at INTEGER,
  instance_id TEXT,
  last_seq INTEGER NOT NULL DEFAULT 0,
  last_snapshot_id TEXT,
  updated_at INTEGER NOT NULL
);
CREATE INDEX idx_test_workers_zone_status ON test_workers(zone, status, expires_at);

ALTER TABLE tasks_active ADD COLUMN reporter_id TEXT;
ALTER TABLE tasks_active ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1));
UPDATE tasks_active SET reporter_id = worker_id WHERE reporter_id IS NULL;
CREATE INDEX idx_active_reporter ON tasks_active(reporter_id, updated_at);
CREATE INDEX idx_active_wf_test ON tasks_active(provider, repo, run_id, attempt, is_test);

ALTER TABLE task_index ADD COLUMN reporter_id TEXT;
ALTER TABLE task_index ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1));
UPDATE task_index SET reporter_id = worker_id WHERE reporter_id IS NULL;
CREATE INDEX idx_index_wf_test ON task_index(provider, repo, run_id, attempt, is_test);

CREATE TABLE workflow_runs_next (
  provider TEXT NOT NULL,
  repo TEXT NOT NULL,
  run_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  is_test INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1)),
  reporter_ids_json TEXT NOT NULL DEFAULT '[]',
  logical_model TEXT NOT NULL,
  submitted_at INTEGER,
  workflow_name TEXT,
  run_number INTEGER,
  display_title TEXT,
  workflow_file TEXT,
  pr_number INTEGER,
  pr_author TEXT,
  trigger_actor TEXT,
  event_type TEXT,
  is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
  has_in_flight INTEGER NOT NULL CHECK (has_in_flight IN (0, 1)),
  has_waiting_resource_case INTEGER NOT NULL CHECK (has_waiting_resource_case IN (0, 1)),
  active_case_count INTEGER NOT NULL,
  total_case_count INTEGER NOT NULL,
  queue_order_rank INTEGER,
  first_started_at INTEGER,
  finished_at INTEGER,
  result_state TEXT NOT NULL CHECK (result_state IN ('QUEUED', 'PASS', 'FAIL')),
  pools_json TEXT NOT NULL,
  max_cards INTEGER NOT NULL,
  card_seconds_finished INTEGER NOT NULL,
  has_unmeasured_allocated INTEGER NOT NULL CHECK (has_unmeasured_allocated IN (0, 1)),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (provider, repo, run_id, attempt, is_test)
);

INSERT INTO workflow_runs_next (
  provider, repo, run_id, attempt, is_test, reporter_ids_json, logical_model, submitted_at,
  workflow_name, run_number, display_title, workflow_file, pr_number, pr_author, trigger_actor, event_type,
  is_active, has_in_flight, has_waiting_resource_case, active_case_count, total_case_count,
  queue_order_rank, first_started_at, finished_at, result_state, pools_json, max_cards,
  card_seconds_finished, has_unmeasured_allocated, updated_at
)
SELECT
  w.provider, w.repo, w.run_id, w.attempt, 0,
  COALESCE((
    SELECT json_group_array(reporter_id) FROM (
      SELECT DISTINCT COALESCE(a.reporter_id, a.worker_id) AS reporter_id
      FROM tasks_active a
      WHERE a.provider = w.provider AND a.repo = w.repo AND a.run_id = w.run_id AND a.attempt = w.attempt
      UNION
      SELECT DISTINCT COALESCE(i.reporter_id, i.worker_id) AS reporter_id
      FROM task_index i
      WHERE i.provider = w.provider AND i.repo = w.repo AND i.run_id = w.run_id AND i.attempt = w.attempt
    )
  ), '[]'),
  w.logical_model, w.submitted_at, w.workflow_name, w.run_number, w.display_title, w.workflow_file,
  w.pr_number, w.pr_author, w.trigger_actor, w.event_type, w.is_active, w.has_in_flight,
  w.has_waiting_resource_case, w.active_case_count, w.total_case_count, w.queue_order_rank,
  w.first_started_at, w.finished_at, w.result_state, w.pools_json, w.max_cards,
  w.card_seconds_finished, w.has_unmeasured_allocated, w.updated_at
FROM workflow_runs w;

DROP TABLE workflow_runs;
ALTER TABLE workflow_runs_next RENAME TO workflow_runs;

CREATE INDEX idx_wf_active_queue ON workflow_runs(is_active, has_in_flight, queue_order_rank, submitted_at);
CREATE INDEX idx_wf_history_page ON workflow_runs(is_active, finished_at DESC, provider, repo, run_id, attempt, is_test);
CREATE INDEX idx_wf_model_history ON workflow_runs(logical_model, is_active, finished_at DESC);
