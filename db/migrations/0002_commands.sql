-- Comandi GitHub da Telegram: richiesta dell'owner, azione interpretata, conferma ed esito.
CREATE TABLE commands (
    id                  INTEGER PRIMARY KEY,
    telegram_message_id INTEGER NOT NULL UNIQUE,  -- messaggio con la richiesta
    confirm_message_id  INTEGER UNIQUE,           -- messaggio "sì" / "no"
    text                TEXT NOT NULL,
    action              TEXT,                     -- JSON validato, null se non interpretabile
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'executed', 'cancelled', 'expired', 'failed', 'rejected')),
    result              TEXT,                     -- JSON (url dell'issue, ...)
    error               TEXT,
    created_at          TEXT NOT NULL,
    decided_at          TEXT
);
CREATE INDEX idx_commands_status ON commands (status, created_at);
