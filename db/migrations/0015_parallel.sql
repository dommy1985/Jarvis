-- Run in parallelo: dipendenze proposte dal planner e run lanciati insieme dall'owner ("vai 1 2 insieme").
ALTER TABLE briefing_items ADD COLUMN depends_on TEXT;       -- JSON: task_id da cui la voce dipende (planner)
ALTER TABLE runs ADD COLUMN parallel_group TEXT;             -- run messi in coda con "insieme": partono insieme
ALTER TABLE runs ADD COLUMN depends_on_run INTEGER REFERENCES runs (id);  -- run su cui questo è impilato (base agent/<id>)
