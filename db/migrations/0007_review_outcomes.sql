-- Fase 3: review incrociata, esiti delle PR, riallineamento del submodule dopo il merge.
ALTER TABLE runs ADD COLUMN review TEXT;        -- JSON: revisore, verdetto, problemi, giri di correzione
ALTER TABLE runs ADD COLUMN sub_pr_state TEXT;  -- JSON: {url: {"state": open|merged|closed, "sha": merge commit}}
ALTER TABLE runs ADD COLUMN checked_at TEXT;    -- ultimo controllo della PR su GitHub
