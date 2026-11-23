-- Metadata captured from the existing platform run response. No additional
-- platform request is needed to populate these fields.
ALTER TABLE tasks_active ADD COLUMN submitted_at INTEGER;
ALTER TABLE tasks_active ADD COLUMN workflow_name TEXT;
ALTER TABLE task_index ADD COLUMN submitted_at INTEGER;
ALTER TABLE task_index ADD COLUMN workflow_name TEXT;
