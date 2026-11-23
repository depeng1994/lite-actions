-- Materialized Workflow identity and coarse scheduling summary. The read
-- projection still expands task_index/tasks_active for case details, while
-- this table gives retention and future queue reads one durable run key.
CREATE TABLE workflow_runs (
  provider TEXT NOT NULL,
  repo TEXT NOT NULL,
  run_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  logical_model TEXT NOT NULL,
  submitted_at INTEGER,
  workflow_name TEXT,
  workflow_file TEXT,
  pr_number INTEGER,
  pr_author TEXT,
  trigger_actor TEXT,
  event_type TEXT,
  is_active INTEGER NOT NULL CHECK(is_active IN (0,1)),
  has_in_flight INTEGER NOT NULL CHECK(has_in_flight IN (0,1)),
  has_waiting_resource_case INTEGER NOT NULL CHECK(has_waiting_resource_case IN (0,1)),
  active_case_count INTEGER NOT NULL,
  total_case_count INTEGER NOT NULL,
  queue_order_rank INTEGER,
  first_started_at INTEGER,
  finished_at INTEGER,
  result_state TEXT NOT NULL CHECK(result_state IN ('QUEUED','PASS','FAIL')),
  pools_json TEXT NOT NULL,
  max_cards INTEGER NOT NULL,
  card_seconds_finished INTEGER NOT NULL,
  has_unmeasured_allocated INTEGER NOT NULL CHECK(has_unmeasured_allocated IN (0,1)),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(provider,repo,run_id,attempt)
);
CREATE INDEX idx_wf_active_queue ON workflow_runs(is_active,has_in_flight,queue_order_rank,submitted_at);
CREATE INDEX idx_wf_history_page ON workflow_runs(is_active,finished_at DESC,provider,repo,run_id,attempt);
CREATE INDEX idx_wf_model_history ON workflow_runs(logical_model,is_active,finished_at DESC);
