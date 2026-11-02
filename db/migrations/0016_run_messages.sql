-- Mailbox fra run in esecuzione insieme: un agente scrive a un altro (jarvis-msg), il runner fa da tramite.
CREATE TABLE run_messages (
    id         INTEGER PRIMARY KEY,
    from_run   INTEGER NOT NULL REFERENCES runs (id),
    to_run     INTEGER NOT NULL REFERENCES runs (id),
    kind       TEXT NOT NULL DEFAULT 'note' CHECK (kind IN ('note', 'ask')),  -- ask: chi scrive aspetta una risposta
    reply_to   INTEGER REFERENCES run_messages (id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at    TEXT
);
CREATE INDEX idx_run_messages_to ON run_messages (to_run, read_at);
