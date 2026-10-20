-- Commenti di review e CI rossa sulle PR aperte dai run: Jarvis propone "ripara N" e li passa all'agente.
ALTER TABLE runs ADD COLUMN feedback TEXT;        -- JSON: run agent = ultimo feedback visto sulle PR; run repair = feedback passato all'agente
ALTER TABLE runs ADD COLUMN feedback_since TEXT;  -- run agent: contano i commenti scritti dopo (default: fine del run)
ALTER TABLE runs ADD COLUMN fallback_from TEXT;   -- esecutore sostituito in partenza perché la sua quota era esaurita
