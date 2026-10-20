-- Schema Jarvis (SQLite), versione 1. Le tabelle di fase 2-3 esistono già e restano vuote finché non servono.
-- Le modifiche successive sono in db/migrations/ (applicate da jarvis.db.migrate).
-- Valori di executor/project/type sono le chiavi di config/projects.yaml.
-- Timestamp in ISO 8601 UTC (TEXT).

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA user_version = 1;

-- Task normalizzati da Jira e GitHub. Upsert su (source, external_id).
CREATE TABLE tasks (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL CHECK (source IN ('jira', 'github')),
    external_id   TEXT NOT NULL,              -- PROJ-412, acme/my-app#31
    project       TEXT,                       -- null se nessun progetto in projects.yaml corrisponde
    type          TEXT,                       -- bugfix, feature, ... (dedotto dal planner se assente)
    title         TEXT NOT NULL,
    description   TEXT,                       -- per i progetti con redact_description non viene passata al planner
    url           TEXT NOT NULL,
    source_status TEXT NOT NULL,              -- stato grezzo della sorgente
    labels        TEXT NOT NULL DEFAULT '[]', -- JSON array
    created_by    TEXT,
    assignee      TEXT,
    eligible      INTEGER NOT NULL DEFAULT 0 CHECK (eligible IN (0, 1)), -- creato dall'owner o con label jarvis
    is_open       INTEGER NOT NULL DEFAULT 1 CHECK (is_open IN (0, 1)),
    content_hash  TEXT NOT NULL,              -- cambia se cambiano titolo/descrizione/stato/label
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    closed_at     TEXT,
    UNIQUE (source, external_id)
);
CREATE INDEX idx_tasks_open ON tasks (is_open, eligible);

-- Un briefing al giorno.
CREATE TABLE briefings (
    id                  INTEGER PRIMARY KEY,
    date                TEXT NOT NULL UNIQUE,   -- YYYY-MM-DD, Europe/Rome
    available_hours     REAL NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'sent', 'confirmed', 'expired', 'failed')),
    planner_executor    TEXT NOT NULL,
    raw_output          TEXT,                   -- JSON restituito dal planner
    error               TEXT,
    telegram_message_id INTEGER,
    created_at          TEXT NOT NULL,
    sent_at             TEXT,
    confirmed_at        TEXT
);

-- Voci del briefing: proposta del planner e decisione finale.
CREATE TABLE briefing_items (
    id                INTEGER PRIMARY KEY,
    briefing_id       INTEGER NOT NULL REFERENCES briefings (id) ON DELETE CASCADE,
    task_id           INTEGER NOT NULL REFERENCES tasks (id),
    position          INTEGER NOT NULL,         -- numero mostrato su Telegram ("2 claude haiku")
    proposed_type     TEXT,
    proposed_executor TEXT NOT NULL,
    estimate_hours    REAL,
    rationale         TEXT,
    final_executor    TEXT,
    decision_source   TEXT CHECK (decision_source IN ('telegram', 'project_rule', 'planner', 'quota_fallback')),
    status            TEXT NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed', 'approved', 'skipped', 'queued', 'done')),
    UNIQUE (briefing_id, position),
    UNIQUE (briefing_id, task_id)
);

-- Risposte Telegram al briefing, grezze e interpretate, per audit.
CREATE TABLE briefing_replies (
    id                  INTEGER PRIMARY KEY,
    briefing_id         INTEGER NOT NULL REFERENCES briefings (id) ON DELETE CASCADE,
    telegram_message_id INTEGER NOT NULL UNIQUE,
    text                TEXT NOT NULL,
    parsed              TEXT,                   -- JSON dei comandi riconosciuti, null se non riconosciuta
    received_at         TEXT NOT NULL
);

-- Esecuzioni (fase 2+) ed esiti (fase 3).
CREATE TABLE runs (
    id                INTEGER PRIMARY KEY,
    task_id           INTEGER NOT NULL REFERENCES tasks (id),
    briefing_item_id  INTEGER REFERENCES briefing_items (id),
    executor          TEXT NOT NULL,
    model             TEXT NOT NULL,
    reviewer_executor TEXT,
    reviewer_model    TEXT,
    branch            TEXT,                     -- agent/<id>
    worktree_path     TEXT,
    pr_url            TEXT,
    status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'review', 'pr_open', 'done', 'failed', 'cancelled')),
    pr_outcome        TEXT CHECK (pr_outcome IN ('approved_as_is', 'approved_with_changes', 'discarded')),
    review_rounds     INTEGER NOT NULL DEFAULT 0,
    rate_limit_hits   INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    queued_at         TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    duration_seconds  INTEGER
);
CREATE INDEX idx_runs_status ON runs (status);

-- Stato quote per CLI: snapshot periodici (agy /usage) ed eventi di esaurimento.
CREATE TABLE quota_events (
    id                 INTEGER PRIMARY KEY,
    cli                TEXT NOT NULL CHECK (cli IN ('claude', 'agy')),
    bucket             TEXT,                    -- gemini-5h, gemini-weekly, 3p-5h, ...
    kind               TEXT NOT NULL CHECK (kind IN ('snapshot', 'exhausted', 'rate_limited')),
    remaining_fraction REAL,
    reset_at           TEXT,
    run_id             INTEGER REFERENCES runs (id),
    observed_at        TEXT NOT NULL
);
CREATE INDEX idx_quota_cli ON quota_events (cli, observed_at);
