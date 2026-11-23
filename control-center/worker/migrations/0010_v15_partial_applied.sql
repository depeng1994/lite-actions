-- Support PARTIAL_APPLIED SYNC_CONFIG receipts when healthy nodes apply a
-- published revision while unreachable/probe-failed nodes are locally isolated.
DROP INDEX IF EXISTS idx_command_pending;
DROP INDEX IF EXISTS idx_command_op;
ALTER TABLE commands RENAME TO commands_v2;
CREATE TABLE commands (
  ordinal INTEGER PRIMARY KEY AUTOINCREMENT, cmd_id TEXT NOT NULL UNIQUE, worker_id TEXT NOT NULL,
  cmd_type TEXT NOT NULL CHECK(cmd_type IN ('MOVE_UP','SYNC_CONFIG','SWAP_WORKFLOW')),
  op_id TEXT, payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','ACKED','APPLIED','PARTIAL_APPLIED','NOOP','CONFLICT','FAILED','EXPIRED')),
  created_by TEXT NOT NULL, created_at INTEGER NOT NULL, acked_at INTEGER, finished_at INTEGER,
  result_json TEXT, error_msg TEXT
);
INSERT INTO commands(ordinal,cmd_id,worker_id,cmd_type,op_id,payload_json,status,created_by,created_at,acked_at,finished_at,result_json,error_msg)
  SELECT ordinal,cmd_id,worker_id,cmd_type,op_id,payload_json,status,created_by,created_at,acked_at,finished_at,result_json,error_msg
  FROM commands_v2;
DROP TABLE commands_v2;
CREATE INDEX idx_command_pending ON commands(worker_id,status,ordinal);
CREATE INDEX idx_command_op ON commands(op_id);
