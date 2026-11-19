-- A resource must be drained before it can be removed from the published
-- configuration. The row remains until RESUME or the guarded DELETE step.
CREATE TABLE cluster_drain_targets (
  target_key TEXT PRIMARY KEY,
  worker_id TEXT NOT NULL CHECK (worker_id IN ('blue','yellow')),
  scope TEXT NOT NULL CHECK (scope IN ('pool','node','device_group')),
  pool_name TEXT NOT NULL,
  host_id TEXT,
  devices_json TEXT,
  revision INTEGER NOT NULL,
  requested_by TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_cluster_drain_worker ON cluster_drain_targets(worker_id,pool_name,scope);
