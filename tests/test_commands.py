import json
import threading
import urllib.request
from pathlib import Path

import pytest

from jarvis import commands, config, db
from jarvis.github import GitHubError
from jarvis.server import App

CONFIG = Path(__file__).resolve().parent / "fixtures" / "projects.yaml"
TG = {"chat_id": -100123, "thread_id": 16, "owner_id": 42}
BOARD = {"owner": "alex-dev", "number": 3, "title": "Personale", "id": "PVT_1", "status_field": "F_1",
         "statuses": {"Todo": "o1", "In Progress": "o2", "Done": "o3"}}


@pytest.fixture
def cfg():
    return config.load(CONFIG)


def raw(**kw):
    base = {"action": "create_issue", "repo": "alex-dev/catch-ball", "issue_number": None, "title": "Logo nuovo",
            "body": "", "labels": [], "assignees": [], "project_owner": None, "project_number": None,
            "status": None, "reply": "r"}
    return {**base, **kw}


class FakeGitHub:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if name == self.fail_on:
            raise GitHubError(f"{name} rotto")

    def projects(self, owner):
        self._call("projects", owner)
        return [BOARD] if owner == "alex-dev" else []

    def create_issue(self, repo, title, body, labels, assignees):
        self._call("create_issue", repo, title)
        return {"html_url": f"https://github.com/{repo}/issues/7", "number": 7, "node_id": "I_7"}

    def issue(self, repo, number):
        self._call("issue", repo, number)
        return {"id": f"I_{number}", "url": f"https://github.com/{repo}/issues/{number}", "title": "t",
                "projectItems": {"nodes": [{"id": "ITEM_9", "project": {"id": "PVT_1"}}] if number == 9 else []}}

    def add_assignees(self, repo, number, assignees):
        self._call("add_assignees", repo, number, assignees)

    def add_to_project(self, project_id, content_id):
        self._call("add_to_project", project_id, content_id)
        return "ITEM_NEW"

    def set_status(self, project_id, item_id, field_id, option_id):
        self._call("set_status", project_id, item_id, field_id, option_id)


def test_validate_create_with_board(cfg):
    a = commands.validate(cfg, raw(project_owner="alex-dev", project_number=3, status="todo",
                                   assignees=["@alex-dev"]), [BOARD])
    assert (a["repo"], a["title"], a["status"], a["assignees"]) == (
        "alex-dev/catch-ball", "Logo nuovo", {"name": "Todo", "id": "o1"}, ["alex-dev"])
    assert a["project"]["id"] == "PVT_1"


@pytest.mark.parametrize("bad, error", [
    (raw(action="none", reply="Quale repo?"), "Quale repo?"),
    (raw(repo="alex-dev/altro"), "non scrivibile"),
    (raw(title=" "), "titolo"),
    (raw(action="set_status", issue_number=4, project_number=3, status="Doing"), "non esiste"),
    (raw(action="set_status", issue_number=4), "board"),
    (raw(action="assign", issue_number=None, assignees=["x"]), "numero"),
    (raw(action="assign", issue_number=4), "a chi"),
    (raw(project_number=99), "non trovata"),
    (raw(assignees=["bad login!"]), "assegnatari"),
])
def test_validate_rejects(cfg, bad, error):
    with pytest.raises(commands.CommandError, match=error):
        commands.validate(cfg, bad, [BOARD])


def test_message_has_only_writable_repos_and_request(cfg):
    text = commands.message(cfg, "apri issue su catch-ball", [BOARD])
    assert "alex-dev/catch-ball (catch-ball)" in text and "acme/my-app (my-app)" in text
    assert text.endswith("<message>\napri issue su catch-ball\n</message>")
    json.dumps(commands.schema())


def test_execute_existing_issue_reuses_project_item(cfg):
    gh = FakeGitHub()
    a = commands.validate(cfg, raw(action="set_status", issue_number=9, project_number=3, status="Done"), [BOARD])
    assert commands.execute(gh, a) == {"url": "https://github.com/alex-dev/catch-ball/issues/9", "status": "Done"}
    assert ("set_status", "PVT_1", "ITEM_9", "F_1", "o3") in gh.calls
    assert not any(c[0] == "add_to_project" for c in gh.calls)


def test_execute_partial_failure_reports_what_was_done(cfg):
    gh = FakeGitHub(fail_on="add_to_project")
    a = commands.validate(cfg, raw(project_number=3, status="Todo"), [BOARD])
    with pytest.raises(GitHubError) as e:
        commands.execute(gh, a)
    assert e.value.partial == {"url": "https://github.com/alex-dev/catch-ball/issues/7", "number": 7}


@pytest.fixture
def server(tmp_path, cfg):
    gh = FakeGitHub()
    outputs = []

    def fake(system, message, schema, model, timeout):
        assert "<message>" in message and model == "sonnet"
        return outputs.pop(0)

    app = App(cfg, str(tmp_path / "c.db"), token="secret", runner=fake, telegram=TG, github=gh)
    srv = app.server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, gh, outputs, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def reply(url, text, message_id):
    body = {"chat_id": TG["chat_id"], "thread_id": TG["thread_id"], "from_id": TG["owner_id"],
            "message_id": message_id, "text": text}
    req = urllib.request.Request(f"{url}/reply", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer secret"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def test_command_flow(server):
    app, gh, outputs, url = server
    outputs.append(raw(project_owner="alex-dev", project_number=3, status="Todo"))
    r = reply(url, "apri issue su catch-ball: logo nuovo, in Todo", 10)
    text = r["message"]["text"]
    assert text.startswith("🛠 <b>Creo un'issue</b> su <code>alex-dev/catch-ball</code>")
    assert "<b>Board:</b> Personale → Todo" in text and "<code>sì</code>" in text
    assert not any(c[0] == "create_issue" for c in gh.calls)  # niente scritture prima del sì
    assert reply(url, "apri issue su catch-ball: logo nuovo, in Todo", 10).get("duplicate")

    done = reply(url, "Sì", 11)["message"]["text"]
    assert done.startswith('✅ Fatto: <a href="https://github.com/alex-dev/catch-ball/issues/7">alex-dev/catch-ball#7</a>')
    assert "aggiunta a Personale · stato Todo" in done
    assert [c[0] for c in gh.calls if c[0] != "projects"] == ["create_issue", "add_to_project", "set_status"]
    assert reply(url, "Sì", 11).get("duplicate")
    assert "Nessuna azione" in reply(url, "si", 12)["message"]["text"]
    task = app.conn.execute("SELECT eligible, title FROM tasks WHERE external_id = 'alex-dev/catch-ball#7'").fetchone()
    assert tuple(task) == (1, "Logo nuovo")  # registrata subito, senza aspettare l'ingest
    row = app.conn.execute("SELECT status, result FROM commands WHERE telegram_message_id = 10").fetchone()
    assert row["status"] == "executed" and json.loads(row["result"])["number"] == 7


def test_command_cancel_and_supersede(server):
    app, gh, outputs, url = server
    outputs += [raw(title="Primo"), raw(title="Secondo")]
    reply(url, "apri issue primo", 20)
    reply(url, "anzi apri issue secondo", 21)
    assert reply(url, "no", 22)["message"]["text"].startswith("❌ Annullato")
    statuses = [r[0] for r in app.conn.execute("SELECT status FROM commands ORDER BY id")]
    assert statuses == ["cancelled", "cancelled"]
    assert not any(c[0] == "create_issue" for c in gh.calls)


def test_command_rejected_and_briefing_like_text(server):
    app, gh, outputs, url = server
    outputs.append(raw(action="none", reply="Su quale repo?"))
    r = reply(url, "apri un'issue", 30)
    assert r["message"]["text"].startswith("🤔 Su quale repo?") and "GitHub:" in r["message"]["text"]
    assert app.conn.execute("SELECT status FROM commands").fetchone()[0] == "rejected"
    # "2 gpt" sembra un comando del briefing: non va a claude
    assert "Nessun briefing" in reply(url, "2 gpt", 31)["message"]["text"]


def test_pending_expires(server, monkeypatch):
    app, gh, outputs, url = server
    outputs.append(raw())
    reply(url, "apri issue", 40)
    app.conn.execute("UPDATE commands SET created_at = '2020-01-01T00:00:00+00:00'")
    assert "Nessuna azione" in reply(url, "sì", 41)["message"]["text"]
    assert app.conn.execute("SELECT status FROM commands").fetchone()[0] == "expired"


def test_yes_with_more_words_confirms_pending(server):
    app, gh, outputs, url = server
    outputs.append(raw())
    reply(url, "apri issue", 60)
    assert reply(url, "si crea issue", 61)["message"]["text"].startswith("✅ Fatto")
    outputs.append(raw())
    reply(url, "apri issue", 62)
    assert reply(url, "no, lascia", 63)["message"]["text"].startswith("❌ Annullato")
    # senza azioni in attesa "si ..." resta una richiesta per l'interprete
    outputs.append(raw(action="none", reply="Su quale repo?"))
    assert reply(url, "si apri issue", 64)["message"]["text"].startswith("🤔")


def test_migration_adds_commands_table(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    conn.execute("SELECT count(*) FROM commands")


def test_answer_to_question_goes_back_to_interpreter(server):
    app, gh, outputs, url = server
    seen = []
    outputs += [raw(action="none", reply="Nessuna board: la apro senza?"), raw()]
    original = app.commands_runner

    def spy(system, message, schema, model, timeout):
        seen.append(message)
        return original(system, message, schema, model, timeout)

    app.commands_runner = spy
    reply(url, "apri issue su catch-ball in Todo", 50)
    r = reply(url, "sì", 51)
    assert r["message"]["text"].startswith("🛠 <b>Creo un'issue</b>")
    assert '"your_reply": "Nessuna board: la apro senza?"' in seen[1] and "<message>\nsì\n</message>" in seen[1]
    assert reply(url, "sì", 52)["message"]["text"].startswith("✅ Fatto")


def test_write_owners_limit_repos(cfg):
    only_personal = config.Config({**cfg.raw, "commands": {**cfg.commands, "write_owners": ["alex-dev"]}})
    with pytest.raises(commands.CommandError, match="non scrivibile"):
        commands.validate(only_personal, raw(repo="acme/my-app"), [BOARD])
    assert commands.validate(cfg, raw(repo="ACME/my-app"), [])["repo"] == "acme/my-app"


def test_tickets_for_clone(tmp_path, cfg):
    from jarvis.ingest import ingest
    conn = db.connect(tmp_path / "t.db")
    ingest(conn, cfg, {"tasks": [
        {"source": "jira", "external_id": "PROJ-412", "container": "PROJ", "title": "Login rotto",
         "description": "segreto", "url": "https://x/browse/PROJ-412", "source_status": "To Do"},
        {"source": "github", "external_id": "acme/my-app#5", "container": "acme/my-app",
         "title": "PROJ-412 Login rotto", "url": "https://github.com/acme/my-app/issues/5",
         "source_status": "OPEN"},
    ]}, "2026-10-13T08:00:00+00:00")
    found = commands.tickets(conn, "clona proj-412 e PROJ-999 su release-6")
    assert found == [{"key": "PROJ-412", "project": "my-app", "title": "Login rotto",
                      "url": "https://x/browse/PROJ-412", "status": "To Do",
                      "github_issues": ["https://github.com/acme/my-app/issues/5"]}]
    text = commands.message(cfg, "clona PROJ-412", [], tickets=found)
    assert '"key": "PROJ-412"' in text and "segreto" not in text  # mai la descrizione del ticket
