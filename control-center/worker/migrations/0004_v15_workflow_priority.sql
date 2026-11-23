-- Versioned Workflow-level priority operations. The command table is rebuilt
-- because SQLite cannot extend an existing CHECK constraint with ALTER TABLE.
CREATE TABLE workflow_queue_meta (
  id INTEGER PRIMARY KEY CHECK (id=1),
  queue_version INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL
);
INSERT INTO workflow_queue_meta(id,queue_version,updated_at) VALUES (1,0,0);

CREATE TABLE workflow_priority_ops (
  op_id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE,
  queue_version_before INTEGER NOT NULL,
  queue_version_after INTEGER NOT NULL,
  target_wf_json TEXT NOT NULL,
  swapped_wf_json TEXT,
  from_seq INTEGER NOT NULL,
  to_seq INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING','NOOP','APPLIED','CONFLICT','FAILED')),
  requested_by TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_workflow_priority_created ON workflow_priority_ops(created_at DESC,op_id);

DROP INDEX IF EXISTS idx_command_pending;
ALTER TABLE commands RENAME TO commands_v1;
CREATE TABLE commands (
  ordinal INTEGER PRIMARY KEY AUTOINCREMENT, cmd_id TEXT NOT NULL UNIQUE, worker_id TEXT NOT NULL,
  cmd_type TEXT NOT NULL CHECK(cmd_type IN ('MOVE_UP','SYNC_CONFIG','SWAP_WORKFLOW')),
  op_id TEXT, payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','ACKED','APPLIED','NOOP','CONFLICT','FAILED','EXPIRED')),
  created_by TEXT NOT NULL, created_at INTEGER NOT NULL, acked_at INTEGER, finished_at INTEGER,
  result_json TEXT, error_msg TEXT
);
INSERT INTO commands(ordinal,cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at,acked_at,finished_at,result_json,error_msg)
  SELECT ordinal,cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at,acked_at,finished_at,result_json,error_msg
  FROM commands_v1;
DROP TABLE commands_v1;
CREATE INDEX idx_command_pending ON commands(worker_id,status,ordinal);
CREATE INDEX idx_command_op ON commands(op_id);
