import json
from pathlib import Path

import pytest

from jarvis import config, db, planner
from jarvis.ingest import ingest

CONFIG = Path(__file__).resolve().parent / "fixtures" / "projects.yaml"


@pytest.fixture
def cfg():
    return config.load(CONFIG)


@pytest.fixture
def conn(tmp_path, cfg):
    c = db.connect(tmp_path / "t.db")
    tasks = [
        {"source": "jira", "external_id": "PROJ-1", "container": "PROJ", "title": "Crash refresh token",
         "description": "dettagli riservati Example", "url": "u", "source_status": "To Do",
         "labels": ["jarvis"], "issue_type": "Bug"},
        {"source": "github", "external_id": "alex-dev/novainfo#31", "container": "alex-dev/novainfo",
         "title": "Aggiorna dipendenze", "description": "usa expo 55", "url": "u", "source_status": "OPEN",
         "labels": ["dependencies"], "created_by": "alex-dev"},
        {"source": "github", "external_id": "acme/my-lib#9",
         "container": "acme/my-lib", "title": "Feature mappa",
         "description": "dettagli Acme", "url": "u", "source_status": "Todo", "labels": [], "created_by": "alex-dev"},
        {"source": "github", "external_id": "alex-dev/catch-ball#2", "container": "alex-dev/catch-ball",
         "title": "In pausa", "url": "u", "source_status": "On Hold", "labels": [], "created_by": "alex-dev"},
        {"source": "github", "external_id": "alex-dev/catch-ball#3", "container": "alex-dev/catch-ball",
         "title": "Di qualcun altro", "url": "u", "source_status": "OPEN", "labels": [], "created_by": "someone"},
    ]
    ingest(c, cfg, {"tasks": tasks}, "2026-10-01T08:00:00+00:00")
    return c


def task_id(conn, ref):
    return conn.execute("SELECT id FROM tasks WHERE external_id = ?", (ref,)).fetchone()[0]


class FakeClaude:
    def __init__(self, output=None, error=None):
        self.output, self.error, self.calls = output, error, []

    def __call__(self, system, message, schema, model, timeout):
        self.calls.append({"system": system, "message": message, "schema": schema, "model": model})
        if self.error:
            raise planner.PlannerError(self.error)
        return self.output


def item(tid, executor="claude-sonnet", type_="bugfix", hours=1):
    return {"task_id": tid, "type": type_, "executor": executor, "estimate_hours": hours, "rationale": "r"}


def test_candidates_filter_and_redact(conn, cfg):
    tasks = {t["ref"]: t for t in planner.candidates(conn, cfg, "2026-10-11")}
    assert set(tasks) == {"PROJ-1", "alex-dev/novainfo#31", "acme/my-lib#9"}
    assert "description" not in tasks["PROJ-1"]  # Jira: sempre senza descrizione
    assert "description" not in tasks["acme/my-lib#9"]  # progetto redact
    assert tasks["alex-dev/novainfo#31"]["description"] == "usa expo 55"
    assert tasks["PROJ-1"]["age_days"] == 10


def test_plan_stores_items_and_applies_project_rule(conn, cfg):
    jira, nova = task_id(conn, "PROJ-1"), task_id(conn, "alex-dev/novainfo#31")
    fake = FakeClaude({"summary": "s", "items": [
        item(jira, executor="claude-haiku"),             # my-app ha override bugfix -> sonnet
        item(nova, executor="antigravity", type_="dipendenze"),
    ]})
    planner.plan(conn, cfg, "2026-10-11", available_hours=2, runner=fake)

    b = planner.briefing(conn, "2026-10-11")
    assert (b["status"], b["available_hours"], b["summary"]) == ("draft", 2, "s")
    assert [(i["position"], i["external_id"], i["proposed_executor"], i["decision_source"]) for i in b["items"]] == [
        (1, "PROJ-1", "claude-sonnet", "project_rule"),
        (2, "alex-dev/novainfo#31", "antigravity", "planner"),
    ]
    assert fake.calls[0]["model"] == "sonnet"
    assert "dettagli riservati" not in fake.calls[0]["message"]
    assert "<tasks>" in fake.calls[0]["message"]


def test_unknown_and_duplicate_ids_are_dropped(conn, cfg):
    nova = task_id(conn, "alex-dev/novainfo#31")
    hidden = task_id(conn, "alex-dev/catch-ball#2")  # on hold: non passato al planner
    fake = FakeClaude({"summary": "s", "items": [item(9999), item(hidden), item(nova), item(nova)]})
    planner.plan(conn, cfg, "2026-10-11", runner=fake)
    assert [i["external_id"] for i in planner.briefing(conn, "2026-10-11")["items"]] == ["alex-dev/novainfo#31"]


def test_rerun_replaces_draft_but_not_sent(conn, cfg):
    nova = task_id(conn, "alex-dev/novainfo#31")
    planner.plan(conn, cfg, "2026-10-11", runner=FakeClaude({"summary": "a", "items": [item(nova)]}))
    planner.plan(conn, cfg, "2026-10-11", runner=FakeClaude({"summary": "b", "items": []}))
    assert planner.briefing(conn, "2026-10-11")["summary"] == "b"
    assert conn.execute("SELECT count(*) FROM briefings").fetchone()[0] == 1

    conn.execute("UPDATE briefings SET status = 'sent'")
    with pytest.raises(planner.BriefingLocked):
        planner.plan(conn, cfg, "2026-10-11", runner=FakeClaude({"summary": "c", "items": []}))
    planner.plan(conn, cfg, "2026-10-11", force=True, runner=FakeClaude({"summary": "c", "items": []}))
    assert planner.briefing(conn, "2026-10-11")["status"] == "draft"


def test_failure_is_recorded(conn, cfg):
    with pytest.raises(planner.PlannerError):
        planner.plan(conn, cfg, "2026-10-11", runner=FakeClaude(error="rate limit"))
    b = planner.briefing(conn, "2026-10-11")
    assert (b["status"], b["error"], b["items"]) == ("failed", "rate limit", [])


def test_no_candidates_skips_claude(tmp_path, cfg):
    fake = FakeClaude()
    planner.plan(db.connect(tmp_path / "e.db"), cfg, "2026-10-11", runner=fake)
    assert fake.calls == []


def test_schema_enums_follow_config(cfg):
    s = planner.schema(cfg)["properties"]["items"]["items"]["properties"]
    assert "me" in s["executor"]["enum"] and "antigravity" in s["executor"]["enum"]
    assert "bugfix" in s["type"]["enum"]
    json.dumps(planner.schema(cfg))


def test_project_rule_does_not_override_me(conn, cfg):
    jira = task_id(conn, "PROJ-1")
    planner.plan(conn, cfg, "2026-10-11", runner=FakeClaude({"summary": "s", "items": [item(jira, executor="me")]}))
    i = planner.briefing(conn, "2026-10-11")["items"][0]
    assert (i["proposed_executor"], i["decision_source"]) == ("me", "planner")


def test_paused_project_is_not_proposed(conn, cfg):
    conn.execute("UPDATE tasks SET source_status = 'OPEN' WHERE external_id = 'alex-dev/catch-ball#2'")
    assert cfg.projects["catch-ball"]["paused"]
    assert "alex-dev/catch-ball#2" not in {t["ref"] for t in planner.candidates(conn, cfg, "2026-10-11")}
    active = config.Config({**cfg.raw, "projects": {**cfg.projects, "catch-ball": {**cfg.projects["catch-ball"], "paused": False}}})
    assert "alex-dev/catch-ball#2" in {t["ref"] for t in planner.candidates(conn, active, "2026-10-11")}
