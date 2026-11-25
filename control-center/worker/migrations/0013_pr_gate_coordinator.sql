CREATE TABLE pr_gate_generations (
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  pr_head_sha TEXT NOT NULL,
  generation INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'PASS', 'FAIL', 'CANCELLED', 'SUPERSEDED')),
  github_run_id TEXT,
  github_run_attempt INTEGER,
  check_run_id TEXT,
  status_context TEXT NOT NULL DEFAULT 'Lite-Actions-PR-Gate',
  required_suites_json TEXT NOT NULL DEFAULT '[]',
  suite_results_json TEXT NOT NULL DEFAULT '{}',
  target_url TEXT,
  summary TEXT,
  author_login TEXT,
  trigger_actor TEXT,
  approved_by TEXT,
  approval_mode TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  finished_at INTEGER,
  PRIMARY KEY (repo, pr_number, pr_head_sha, generation)
);
CREATE INDEX idx_pr_gate_pr_status ON pr_gate_generations(repo, pr_number, status, updated_at DESC);

CREATE TABLE gitcode_dispatch_intents (
  intent_id TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  pr_head_sha TEXT NOT NULL,
  generation INTEGER NOT NULL,
  github_run_id TEXT NOT NULL,
  github_run_attempt INTEGER NOT NULL,
  target_test_id TEXT NOT NULL,
  gitcode_repo TEXT NOT NULL,
  gitcode_workflow TEXT NOT NULL,
  gitcode_ref TEXT NOT NULL,
  gitcode_run_id TEXT,
  status TEXT NOT NULL CHECK (status IN (
    'INTENT_CREATED',
    'DISPATCHED',
    'RUNNING',
    'COMPLETED',
    'CANCEL_REQUESTED',
    'CANCELLED',
    'NEEDS_RECONCILIATION',
    'EXPIRED',
    'FAILED'
  )),
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX idx_gitcode_intents_active ON gitcode_dispatch_intents(status, updated_at);
CREATE INDEX idx_gitcode_intents_pr ON gitcode_dispatch_intents(repo, pr_number, pr_head_sha);
