from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from jarvis import checks, config, db
from jarvis.github import GitHubError
from jarvis.ingest import ingest

CONFIG = Path(__file__).resolve().parent / "fixtures" / "projects.yaml"
NOW = datetime(2026, 10, 13, 8, 10, tzinfo=timezone.utc)


class Token:
    def __init__(self, expiration=None, error=None):
        self.expiration, self.error = expiration, error

    def token_expiration(self):
        if self.error:
            raise GitHubError(self.error)
        return self.expiration


def test_stale_ingest(tmp_path):
    conn = db.connect(tmp_path / "s.db")
    assert len(checks.stale_ingest(conn, NOW, config.load(CONFIG))) == 2  # mai registrato
    task = {"source": "jira", "external_id": "PROJ-1", "container": "PROJ", "title": "t", "url": "u",
            "source_status": "To Do"}
    ingest(conn, config.load(CONFIG), {"tasks": [task], "complete_sources": ["jira"]}, "2026-10-13T07:00:00+00:00")
    assert checks.stale_ingest(conn, NOW, config.load(CONFIG)) == ["Nessun ingest github registrato: controlla il workflow jarvis-ingest in n8n."]
    checks.record_ingest(conn, "github", "2026-10-12T20:00:00+00:00")
    assert checks.stale_ingest(conn, NOW, config.load(CONFIG))[0].startswith("Ultimo ingest github riuscito il 12/10 20:00")


@pytest.mark.parametrize("token, expected", [
    (None, []),
    (Token(None), []),
    (Token("2026-11-30"), []),
    (Token("2026-10-20"), ["scade tra 7 giorni (20/10/2026)"]),
    (Token("2026-10-13"), ["scade oggi"]),
    (Token("2026-10-01"), ["è scaduto"]),
    (Token(error="HTTP 401"), ["non funziona (HTTP 401)"]),
])
def test_github_token(token, expected):
    notes = checks.github_token(token, date(2026, 10, 13))
    assert len(notes) == len(expected) and all(e in n for e, n in zip(expected, notes))


def test_planner_hint():
    assert "claude setup-token" in checks.planner_hint("claude -p error: OAuth token has expired")
    assert checks.planner_hint("claude -p oltre 300s") is None


def test_no_stale_ingest_warning_outside_the_ingest_hours():
    cfg = config.load(CONFIG)
    rome = cfg.tz
    last = datetime(2026, 10, 18, 20, 0, tzinfo=rome)  # ultimo ingest della giornata
    assert checks.missed_ingest_runs(last, datetime(2026, 10, 18, 23, 20, tzinfo=rome), cfg) == 0
    assert checks.missed_ingest_runs(last, datetime(2026, 10, 19, 8, 10, tzinfo=rome), cfg) == 2  # 7 e 8: ancora ok
    assert checks.missed_ingest_runs(last, datetime(2026, 10, 19, 9, 5, tzinfo=rome), cfg) == 3


def test_config_owner_defaults_and_validation(tmp_path):
    import yaml

    raw = yaml.safe_load(CONFIG.read_text())
    raw["owner"] = {"github": "x"}
    raw["ingest"].pop("hours", None)
    cfg = config.Config(raw)
    assert cfg.owner_name == "Owner" and cfg.owner_desc == "Owner" and str(cfg.tz) == "UTC"
    assert list(cfg.ingest_hours) == list(range(7, 21))
    assert cfg.fill("{owner_desc} / {owner}") == "Owner / Owner"
    raw["owner"] |= {"name": "Ada", "profile": "dev", "timezone": "Europe/Rome"}
    assert config.Config(raw).fill("{owner_desc} / {owner}") == "Ada (dev) / Ada"
    for bad in ({"timezone": "Mars/Base"}, {"name": 3}):
        raw["owner"] = {"github": "x", **bad}
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.safe_dump(raw))
        with pytest.raises(config.ConfigError):
            config.load(path)
    raw["owner"] = {"github": "x"}
    raw["ingest"]["hours"] = [9, 3]
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(config.ConfigError):
        config.load(path)
