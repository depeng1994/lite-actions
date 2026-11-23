-- Preserve the concrete platform run title for compact Actions links.
ALTER TABLE tasks_active ADD COLUMN display_title TEXT;
ALTER TABLE task_index ADD COLUMN display_title TEXT;
ALTER TABLE workflow_runs ADD COLUMN display_title TEXT;
