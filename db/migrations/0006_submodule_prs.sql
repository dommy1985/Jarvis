-- Fase 2: un run su un progetto con submodule apre anche una PR per ogni submodule cambiato.
ALTER TABLE runs ADD COLUMN sub_pr_urls TEXT;  -- JSON: lista delle PR sui submodule (pr_url resta quella dell'app)
