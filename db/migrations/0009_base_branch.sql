-- Branch di base del run (override di "vai N base:<branch>" o base_branch del progetto): il worktree parte da
-- origin/<base_branch> e la PR draft punta lì. NULL = branch di default del repo.
ALTER TABLE runs ADD COLUMN base_branch TEXT;
