-- Fase 2: runner sul Mac. Dettagli dell'esecuzione e battito per riconoscere un runner sparito.
ALTER TABLE runs ADD COLUMN summary TEXT;          -- riepilogo dell'agente (finisce nella PR)
ALTER TABLE runs ADD COLUMN tests TEXT;            -- passed, failed, skipped
ALTER TABLE runs ADD COLUMN step TEXT;             -- worktree, setup, agent, tests, push, pr
ALTER TABLE runs ADD COLUMN heartbeat_at TEXT;
ALTER TABLE runs ADD COLUMN notified_status TEXT;  -- ultimo stato già notificato su Telegram
