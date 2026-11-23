-- Preserve the first execution timestamp after an active task is archived.
-- Older PASS rows can be reconstructed from their measured duration.
ALTER TABLE task_index ADD COLUMN started_at INTEGER;
UPDATE task_index
SET started_at = finished_at - duration_sec
WHERE started_at IS NULL AND duration_sec IS NOT NULL AND duration_sec >= 0;
