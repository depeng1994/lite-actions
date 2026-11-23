-- v1.5 device visibility health.  The columns are additive so existing
-- v1.1 task, lease, and occupancy records remain readable during rollout.
ALTER TABLE workers ADD COLUMN last_visibility_probe_at INTEGER;
ALTER TABLE device_group_states ADD COLUMN visibility_state TEXT NOT NULL DEFAULT 'pending'
  CHECK (visibility_state IN ('pending','online','warn','offline'));
ALTER TABLE device_group_states ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0
  CHECK (consecutive_failures BETWEEN 0 AND 5);
ALTER TABLE device_group_states ADD COLUMN last_online_at INTEGER;
ALTER TABLE device_group_states ADD COLUMN last_probe_round INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_device_health ON device_group_states(worker_id,visibility_state,last_probe_round);
