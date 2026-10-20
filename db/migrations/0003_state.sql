-- Stato chiave/valore del servizio, es. ultimo ingest riuscito per sorgente (last_ingest:jira).
CREATE TABLE state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
