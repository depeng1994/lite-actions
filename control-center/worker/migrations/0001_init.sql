CREATE TABLE workers (
  worker_id TEXT PRIMARY KEY CHECK (worker_id IN ('blue', 'yellow')),
  instance_id TEXT NOT NULL, applied_rev INTEGER NOT NULL, code_sha TEXT NOT NULL,
  health_ok INTEGER NOT NULL, active_count INTEGER NOT NULL, queued_count INTEGER NOT NULL,
  last_heartbeat_at INTEGER NOT NULL, last_idle_probe_at INTEGER, last_egress_used INTEGER NOT NULL,
  need_snapshot INTEGER NOT NULL DEFAULT 1, updated_at INTEGER NOT NULL
);
CREATE TABLE worker_ingest_state (
  worker_id TEXT PRIMARY KEY REFERENCES workers, instance_id TEXT NOT NULL,
  last_seq INTEGER NOT NULL, updated_at INTEGER NOT NULL, last_snapshot_id TEXT
);
CREATE TABLE worker_event_dedup (
  worker_id TEXT NOT NULL, instance_id TEXT NOT NULL, seq INTEGER NOT NULL, received_at INTEGER NOT NULL,
  PRIMARY KEY(worker_id,instance_id,seq)
);
CREATE INDEX idx_dedup_age ON worker_event_dedup(received_at);
CREATE TABLE snapshot_staging (
  worker_id TEXT NOT NULL, instance_id TEXT NOT NULL, snap_id TEXT NOT NULL,
  part_no INTEGER NOT NULL, total_parts INTEGER NOT NULL, seq INTEGER NOT NULL,
  payload_json TEXT NOT NULL, created_at INTEGER NOT NULL,
  PRIMARY KEY(worker_id,instance_id,snap_id,part_no)
);
CREATE INDEX idx_staging_age ON snapshot_staging(created_at);

-- A rollback creates a new revision with the same content hash as an older revision.
CREATE TABLE pools_configs (
  revision INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL, config_json TEXT NOT NULL,
  diff_summary_json TEXT NOT NULL DEFAULT '{}', created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
  rollback_from_rev INTEGER REFERENCES pools_configs(revision)
);
CREATE INDEX idx_pools_sha ON pools_configs(sha256);
CREATE TABLE device_group_states (
  worker_id TEXT NOT NULL, pool_name TEXT NOT NULL, host_id TEXT NOT NULL, devices_json TEXT NOT NULL,
  occupancy_state TEXT CHECK (occupancy_state IN ('idle','busy','probe_error')), probe_at INTEGER,
  quarantined INTEGER NOT NULL DEFAULT 0, quarantine_task_uid TEXT, updated_at INTEGER NOT NULL,
  PRIMARY KEY(worker_id,pool_name,host_id,devices_json)
);
CREATE TABLE tasks_active (
  task_uid TEXT PRIMARY KEY, short_tid TEXT NOT NULL, task_key TEXT NOT NULL, worker_id TEXT NOT NULL,
  provider TEXT NOT NULL, repo TEXT NOT NULL, logical_model TEXT NOT NULL, run_id TEXT NOT NULL,
  attempt INTEGER NOT NULL, dispatch_id TEXT, case_id TEXT NOT NULL, test_id TEXT NOT NULL, label TEXT NOT NULL,
  kind TEXT NOT NULL, target_pool TEXT NOT NULL, req_nodes INTEGER NOT NULL, req_devices INTEGER NOT NULL,
  pr_number INTEGER, pr_author TEXT, trigger_actor TEXT, event_type TEXT, workflow_file TEXT,
  phase TEXT NOT NULL CHECK (phase IN ('QUEUED','RESERVED','STAGING','RUNNING','CLEANING')),
  allocation_json TEXT NOT NULL DEFAULT '[]', queued_at INTEGER, reserved_at INTEGER, staging_at INTEGER,
  running_at INTEGER, cleaning_at INTEGER, timing_reliable INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  UNIQUE (worker_id,short_tid)
);
CREATE INDEX idx_active_model ON tasks_active(logical_model,queued_at);
CREATE INDEX idx_active_run ON tasks_active(provider,repo,run_id,attempt);
CREATE TABLE task_index (
  task_uid TEXT PRIMARY KEY, finished_at INTEGER NOT NULL, worker_id TEXT NOT NULL,
  provider TEXT NOT NULL, repo TEXT NOT NULL, logical_model TEXT NOT NULL, run_id TEXT NOT NULL,
  attempt INTEGER NOT NULL, dispatch_id TEXT, case_id TEXT NOT NULL, test_id TEXT NOT NULL, label TEXT NOT NULL,
  kind TEXT NOT NULL, target_pool TEXT NOT NULL, req_nodes INTEGER NOT NULL, req_devices INTEGER NOT NULL,
  actual_allocation_json TEXT NOT NULL, pr_number INTEGER, pr_author TEXT, trigger_actor TEXT, event_type TEXT,
  workflow_file TEXT, status TEXT NOT NULL, exit_code INTEGER, cleanup_verified INTEGER, duration_sec INTEGER,
  timing_reliable INTEGER NOT NULL, storage_tier TEXT NOT NULL DEFAULT 'hot' CHECK(storage_tier IN ('hot','cold')),
  cold_chunk_id TEXT REFERENCES task_details_cold(chunk_id)
);
CREATE INDEX idx_task_index_model_page ON task_index(logical_model,finished_at DESC,task_uid DESC);
CREATE INDEX idx_task_index_purge ON task_index(finished_at,task_uid);
CREATE INDEX idx_task_index_run ON task_index(provider,repo,run_id,attempt);
CREATE INDEX idx_task_index_archive ON task_index(storage_tier,finished_at);
CREATE INDEX idx_task_index_cold ON task_index(cold_chunk_id);
CREATE TABLE task_details_hot (
  task_uid TEXT PRIMARY KEY REFERENCES task_index ON DELETE CASCADE, finished_at INTEGER NOT NULL, detail_json TEXT NOT NULL
);
CREATE TABLE task_details_cold (
  chunk_id TEXT PRIMARY KEY, logical_model TEXT NOT NULL, finish_day INTEGER NOT NULL, min_finished_at INTEGER NOT NULL,
  max_finished_at INTEGER NOT NULL, record_count INTEGER NOT NULL, sha256 TEXT NOT NULL, gzip_blob BLOB NOT NULL,
  raw_bytes INTEGER NOT NULL, compressed_bytes INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE queue_orders (
  worker_id TEXT NOT NULL, pool_name TEXT NOT NULL, kind TEXT NOT NULL, task_uid TEXT NOT NULL,
  queue_pos INTEGER NOT NULL, candidate_group_idxs_json TEXT NOT NULL, queue_ver INTEGER NOT NULL,
  updated_at INTEGER NOT NULL, PRIMARY KEY(worker_id,pool_name,kind,task_uid)
);
CREATE INDEX idx_queue_task ON queue_orders(task_uid);
CREATE TABLE duration_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT, logical_model TEXT NOT NULL, test_id TEXT NOT NULL, target_pool TEXT NOT NULL,
  req_nodes INTEGER NOT NULL, req_devices INTEGER NOT NULL, duration_sec INTEGER NOT NULL, finished_at INTEGER NOT NULL,
  task_uid TEXT NOT NULL UNIQUE REFERENCES task_index ON DELETE CASCADE
);
CREATE INDEX idx_duration_shape ON duration_samples(logical_model,test_id,target_pool,req_nodes,req_devices,finished_at DESC,id DESC);
CREATE TABLE commands (
  ordinal INTEGER PRIMARY KEY AUTOINCREMENT, cmd_id TEXT NOT NULL UNIQUE, worker_id TEXT NOT NULL,
  cmd_type TEXT NOT NULL CHECK(cmd_type IN ('MOVE_UP','SYNC_CONFIG')), payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','ACKED','APPLIED','NOOP','CONFLICT','FAILED','EXPIRED')),
  created_by TEXT NOT NULL, created_at INTEGER NOT NULL, acked_at INTEGER, finished_at INTEGER, result_json TEXT, error_msg TEXT
);
CREATE INDEX idx_command_pending ON commands(worker_id,status,ordinal);
CREATE TABLE cancellations (
  cancel_id TEXT PRIMARY KEY, provider TEXT NOT NULL, repo TEXT NOT NULL, run_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  status TEXT NOT NULL, requested_by TEXT NOT NULL, created_at INTEGER NOT NULL, submitted_at INTEGER,
  force_cancel_sent_at INTEGER, platform_conclusion TEXT, platform_error TEXT, updated_at INTEGER NOT NULL,
  UNIQUE(provider,repo,run_id,attempt)
);
CREATE INDEX idx_cancel_active ON cancellations(status,updated_at);
CREATE TABLE users (
  user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, role TEXT NOT NULL CHECK(role='admin'),
  key_hash TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER
);
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY, principal_type TEXT NOT NULL, user_id TEXT REFERENCES users,
  username TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('super','admin')),
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER
);
CREATE INDEX idx_sessions_user ON sessions(user_id,revoked_at);
CREATE INDEX idx_sessions_age ON sessions(expires_at);
CREATE TABLE login_attempts (
  bucket_key TEXT PRIMARY KEY, fail_count INTEGER NOT NULL, window_start_at INTEGER NOT NULL, locked_until INTEGER
);
CREATE TABLE audit_logs (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, actor_username TEXT NOT NULL, actor_role TEXT NOT NULL,
  action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL, summary_json TEXT NOT NULL DEFAULT '{}',
  result TEXT NOT NULL, error_msg TEXT, ip_hash TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE system_meta (
  meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL, updated_at INTEGER NOT NULL
);
