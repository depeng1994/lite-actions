-- Keep the platform workflow number available for compact Actions links.
ALTER TABLE tasks_active ADD COLUMN run_number INTEGER;
ALTER TABLE task_index ADD COLUMN run_number INTEGER;
ALTER TABLE workflow_runs ADD COLUMN run_number INTEGER;
