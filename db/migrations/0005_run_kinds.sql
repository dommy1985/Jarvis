-- Fase 2: run di aggiornamento submodule e stop da Telegram.
ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent';         -- agent, bump
ALTER TABLE runs ADD COLUMN project TEXT;                               -- progetto del run se diverso da quello del task (bump)
ALTER TABLE runs ADD COLUMN parent_run_id INTEGER REFERENCES runs (id); -- bump: run che ha aperto la PR sul submodule
ALTER TABLE runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
