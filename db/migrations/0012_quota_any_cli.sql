-- quota_events per qualunque provider (copilot e quelli futuri), non solo claude e agy: SQLite non cambia un CHECK,
-- quindi la tabella si ricrea. Nessuna tabella la referenzia.
CREATE TABLE quota_events_new (
    id                 INTEGER PRIMARY KEY,
    cli                TEXT NOT NULL CHECK (cli <> ''),
    bucket             TEXT,
    kind               TEXT NOT NULL CHECK (kind IN ('snapshot', 'exhausted', 'rate_limited')),
    remaining_fraction REAL,
    reset_at           TEXT,
    run_id             INTEGER REFERENCES runs (id),
    observed_at        TEXT NOT NULL
);
INSERT INTO quota_events_new SELECT id, cli, bucket, kind, remaining_fraction, reset_at, run_id, observed_at FROM quota_events;
DROP TABLE quota_events;
ALTER TABLE quota_events_new RENAME TO quota_events;
CREATE INDEX idx_quota_cli ON quota_events (cli, observed_at);
