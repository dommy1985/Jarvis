-- Runner Mac + VPS: dove gira un run. target: override per run (mac|vps|auto), NULL = quello del progetto.
ALTER TABLE runs ADD COLUMN target TEXT CHECK (target IN ('mac', 'vps', 'auto'));
ALTER TABLE runs ADD COLUMN runner_id TEXT;   -- chi ha preso il run (mac|vps)
