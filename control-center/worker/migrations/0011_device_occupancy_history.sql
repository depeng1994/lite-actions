CREATE TABLE device_occupancy_readings (
  worker_id TEXT NOT NULL,
  pool_name TEXT NOT NULL,
  host_id TEXT NOT NULL,
  devices_json TEXT NOT NULL,
  observed_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('busy','idle','probe_error','leased')),
  PRIMARY KEY (worker_id,pool_name,host_id,devices_json)
);

CREATE TABLE device_occupancy_intervals (
  worker_id TEXT NOT NULL,
  pool_name TEXT NOT NULL,
  host_id TEXT NOT NULL,
  devices_json TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  last_busy_at INTEGER NOT NULL,
  ended_at INTEGER,
  close_reason TEXT CHECK (close_reason IN ('idle','unobserved')),
  PRIMARY KEY (worker_id,pool_name,host_id,devices_json,started_at)
);
CREATE UNIQUE INDEX idx_device_occupancy_open
  ON device_occupancy_intervals(worker_id,pool_name,host_id,devices_json)
  WHERE close_reason IS NULL;
CREATE INDEX idx_device_occupancy_window
  ON device_occupancy_intervals(COALESCE(ended_at,last_busy_at));

CREATE TRIGGER device_occupancy_first_reading
AFTER INSERT ON device_occupancy_readings
WHEN NEW.state='busy'
BEGIN
  INSERT INTO device_occupancy_intervals(worker_id,pool_name,host_id,devices_json,started_at,last_busy_at)
  VALUES (NEW.worker_id,NEW.pool_name,NEW.host_id,NEW.devices_json,NEW.observed_at,NEW.observed_at);
END;

CREATE TRIGGER device_occupancy_next_reading
AFTER UPDATE ON device_occupancy_readings
BEGIN
  -- A gap longer than two 120-second polls plus probe time breaks the evidence.
  UPDATE device_occupancy_intervals
  SET ended_at=CASE WHEN NEW.state='idle' AND NEW.observed_at-OLD.observed_at<=300 THEN NEW.observed_at END,
      close_reason=CASE WHEN NEW.state='idle' AND NEW.observed_at-OLD.observed_at<=300 THEN 'idle' ELSE 'unobserved' END
  WHERE worker_id=NEW.worker_id AND pool_name=NEW.pool_name AND host_id=NEW.host_id AND devices_json=NEW.devices_json
    AND close_reason IS NULL AND (NEW.state<>'busy' OR NEW.observed_at-OLD.observed_at>300);

  UPDATE device_occupancy_intervals SET last_busy_at=NEW.observed_at
  WHERE worker_id=NEW.worker_id AND pool_name=NEW.pool_name AND host_id=NEW.host_id AND devices_json=NEW.devices_json
    AND close_reason IS NULL AND NEW.state='busy';

  INSERT INTO device_occupancy_intervals(worker_id,pool_name,host_id,devices_json,started_at,last_busy_at)
  SELECT NEW.worker_id,NEW.pool_name,NEW.host_id,NEW.devices_json,NEW.observed_at,NEW.observed_at
  WHERE NEW.state='busy' AND NOT EXISTS (
    SELECT 1 FROM device_occupancy_intervals
    WHERE worker_id=NEW.worker_id AND pool_name=NEW.pool_name AND host_id=NEW.host_id AND devices_json=NEW.devices_json
      AND close_reason IS NULL
  );
END;
