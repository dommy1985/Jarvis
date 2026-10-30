-- "N salta" ricorda: il task non torna nel briefing fino a snoozed_until; dopo qualche salto Jarvis chiede se
-- toglierlo ("N mai" -> planner_excluded). L'ingest non scrive queste colonne.
ALTER TABLE tasks ADD COLUMN snoozed_until TEXT;  -- data (YYYY-MM-DD) da cui il planner può riproporlo
ALTER TABLE tasks ADD COLUMN skip_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN planner_excluded INTEGER NOT NULL DEFAULT 0 CHECK (planner_excluded IN (0, 1));

-- Le riserve del planner: i task che non entravano nelle ore, nell'ordine in cui prendono il posto di una voce saltata.
CREATE TABLE briefing_reserves (
    id                INTEGER PRIMARY KEY,
    briefing_id       INTEGER NOT NULL REFERENCES briefings (id) ON DELETE CASCADE,
    task_id           INTEGER NOT NULL REFERENCES tasks (id),
    rank              INTEGER NOT NULL,
    proposed_type     TEXT,
    proposed_executor TEXT NOT NULL,
    estimate_hours    REAL,
    rationale         TEXT,
    decision_source   TEXT CHECK (decision_source IN ('project_rule', 'planner')),
    used              INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0, 1)),
    UNIQUE (briefing_id, task_id)
);
