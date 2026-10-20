-- Briefing concentrato su alcuni progetti ("ore 3 su other-app", "briefing su my-app"): JSON con i
-- nomi dei progetti scelti (chiavi di projects.yaml). NULL = nessun focus, tutti i progetti.
ALTER TABLE briefings ADD COLUMN focus TEXT;
