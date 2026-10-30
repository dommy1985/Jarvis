from pathlib import Path

import pytest

from jarvis import config, db
from jarvis.ingest import IngestError, ingest

CONFIG = Path(__file__).resolve().parent / "fixtures" / "projects.yaml"
JIRA_ME = "acc-me"


@pytest.fixture
def cfg():
    return config.load(CONFIG)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def jira(key="PROJ-1", **kw):
    return {
        "source": "jira", "external_id": key, "container": key.split("-")[0],
        "title": "Crash su refresh token", "url": f"https://x/browse/{key}",
        "source_status": "To Do", "labels": [], "created_by": "acc-pm", "issue_type": "Bug", **kw,
    }


def gh(repo="alex-dev/novainfo", number=31, **kw):
    return {
        "source": "github", "external_id": f"{repo}#{number}", "container": repo,
        "title": "Aggiorna dipendenze", "url": f"https://github.com/{repo}/issues/{number}",
        "source_status": "OPEN", "labels": ["dependencies"], "created_by": "alex-dev", **kw,
    }


def payload(*tasks, complete=("jira", "github")):
    return {"identities": {"jira": JIRA_ME}, "complete_sources": list(complete), "tasks": list(tasks)}


def rows(conn):
    return {r["external_id"]: r for r in conn.execute("SELECT * FROM tasks")}


def test_insert_maps_project_type_and_eligibility(conn, cfg):
    counts = ingest(conn, cfg, payload(jira(), gh(), jira("DPS-7", created_by=JIRA_ME)), "t1")
    assert counts == {"inserted": 3, "updated": 0, "unchanged": 0, "closed": 0}
    r = rows(conn)
    assert (r["PROJ-1"]["project"], r["PROJ-1"]["type"], r["PROJ-1"]["eligible"]) == ("my-app", "bugfix", 0)
    assert (r["alex-dev/novainfo#31"]["project"], r["alex-dev/novainfo#31"]["type"]) == ("novainfo", "dipendenze")
    assert r["alex-dev/novainfo#31"]["eligible"] == 1
    assert (r["DPS-7"]["project"], r["DPS-7"]["eligible"]) == (None, 1)


def test_label_makes_task_eligible(conn, cfg):
    ingest(conn, cfg, payload(jira(labels=["jarvis"])), "t1")
    assert rows(conn)["PROJ-1"]["eligible"] == 1


def test_reingest_is_idempotent(conn, cfg):
    ingest(conn, cfg, payload(jira(), gh()), "t1")
    counts = ingest(conn, cfg, payload(jira(), gh()), "t2")
    assert counts == {"inserted": 0, "updated": 0, "unchanged": 2, "closed": 0}
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
    assert rows(conn)["PROJ-1"]["updated_at"] == "t1"


def test_content_change_updates(conn, cfg):
    ingest(conn, cfg, payload(jira()), "t1")
    counts = ingest(conn, cfg, payload(jira(source_status="In Progress")), "t2")
    assert counts["updated"] == 1
    assert rows(conn)["PROJ-1"]["updated_at"] == "t2"


def test_missing_from_snapshot_closes_and_reappearing_reopens(conn, cfg):
    ingest(conn, cfg, payload(jira(), jira("PROJ-2"), gh()), "t1")
    counts = ingest(conn, cfg, payload(jira(), gh()), "t2")
    assert counts["closed"] == 1
    assert (rows(conn)["PROJ-2"]["is_open"], rows(conn)["PROJ-2"]["closed_at"]) == (0, "t2")

    ingest(conn, cfg, payload(jira(), jira("PROJ-2"), gh()), "t3")
    assert (rows(conn)["PROJ-2"]["is_open"], rows(conn)["PROJ-2"]["closed_at"]) == (1, None)


def test_complete_source_with_no_tasks_closes_its_last_ones(conn, cfg):
    ingest(conn, cfg, payload(jira(), gh()), "t1")
    counts = ingest(conn, cfg, payload(jira()), "t2")
    assert counts["closed"] == 1
    assert rows(conn)["alex-dev/novainfo#31"]["is_open"] == 0
    assert rows(conn)["PROJ-1"]["is_open"] == 1


def test_allow_empty_snapshot_closes_everything(conn, cfg):
    ingest(conn, cfg, payload(jira(), gh()), "t1")
    counts = ingest(conn, cfg, {**payload(), "allow_empty": True}, "t2")
    assert counts["closed"] == 2
    assert all(r["is_open"] == 0 for r in rows(conn).values())


def test_only_complete_sources_are_closed(conn, cfg):
    ingest(conn, cfg, payload(jira(), gh()), "t1")
    ingest(conn, cfg, payload(jira(), complete=["jira"]), "t2")
    assert rows(conn)["alex-dev/novainfo#31"]["is_open"] == 1


def test_empty_snapshot_rejected(conn, cfg):
    ingest(conn, cfg, payload(jira()), "t1")
    with pytest.raises(IngestError):
        ingest(conn, cfg, payload(), "t2")
    assert rows(conn)["PROJ-1"]["is_open"] == 1


def test_invalid_task_rolls_back_whole_batch(conn, cfg):
    with pytest.raises(IngestError):
        ingest(conn, cfg, payload(jira(), {"source": "jira"}), "t1")
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_sources_queries(cfg):
    s = cfg.sources()
    assert "repo:acme/my-app" in s["github_queries"]["team"]
    assert "assignee:" not in s["github_queries"]["team"]
    assert "repo:alex-dev/orbitBe" in s["github_queries"]["personal"]
    assert "assignee:" not in s["github_queries"]["personal"]  # repo personali: tutte le issue aperte
    only_mine = config.Config({**cfg.raw, "ingest": {**cfg.raw["ingest"], "personal_assignee_only": True}})
    assert only_mine.sources()["github_queries"]["personal"].endswith("assignee:alex-dev")


def test_jira_assigned_to_me_is_eligible_only_on_known_projects(conn, cfg):
    ingest(conn, cfg, payload(jira(assignee=JIRA_ME), jira("RVAPT-3", assignee=JIRA_ME), gh(created_by="x", assignee="alex-dev")), "t1")
    r = rows(conn)
    assert r["PROJ-1"]["eligible"] == 1
    assert (r["RVAPT-3"]["project"], r["RVAPT-3"]["eligible"]) == (None, 0)
    assert r["alex-dev/novainfo#31"]["eligible"] == 0  # GitHub: l'assegnazione non basta
