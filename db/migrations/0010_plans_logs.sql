-- Piano prima dell'esecuzione ("vai 4 piano") e log dal vivo dell'agente ("log", "segui").
ALTER TABLE runs ADD COLUMN plan_text TEXT;   -- kind='plan': il piano proposto dall'agente
ALTER TABLE runs ADD COLUMN plan_state TEXT;  -- kind='plan': proposed, approved, revised, discarded
ALTER TABLE runs ADD COLUMN plan_of INTEGER REFERENCES runs (id);  -- agent: piano approvato; plan: piano da rivedere
ALTER TABLE runs ADD COLUMN plan_note TEXT;   -- plan rigenerato: cosa ha chiesto di cambiare l'owner

-- Righe di log (azioni dell'agente, non contenuto dei file): le ultime per run, ripulite dopo qualche giorno.
CREATE TABLE run_log (
    id     INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    at     TEXT NOT NULL,
    line   TEXT NOT NULL
);
CREATE INDEX idx_run_log_run ON run_log (run_id, id);
