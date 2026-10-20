import json
import threading
import urllib.request
from pathlib import Path

import pytest

from jarvis import briefing, config, planner
from jarvis.ingest import ingest
from jarvis.server import App

CONFIG = Path(__file__).resolve().parent / "fixtures" / "projects.yaml"
TG = {"chat_id": -100123, "thread_id": 16, "owner_id": 42}


@pytest.fixture
def cfg():
    return config.load(CONFIG)


def test_parse_commands(cfg):
    assert briefing.parse("OK", cfg) == [{"cmd": "confirm"}]
    assert briefing.parse("2 haiku, 3 io\n1 salta; ok", cfg) == [
        {"cmd": "executor", "position": 2, "executor": "claude-haiku"},
        {"cmd": "executor", "position": 3, "executor": "me"},
        {"cmd": "skip", "position": 1},
        {"cmd": "confirm"},
    ]
    assert briefing.parse("1 sonnet ok", cfg) == [
        {"cmd": "executor", "position": 1, "executor": "claude-sonnet"}, {"cmd": "confirm"},
    ]
    assert briefing.parse("1 claude opus 2 io 3 salta va bene", cfg) == [
        {"cmd": "executor", "position": 1, "executor": "claude-opus"},
        {"cmd": "executor", "position": 2, "executor": "me"},
        {"cmd": "skip", "position": 3},
        {"cmd": "confirm"},
    ]
    assert briefing.parse("1 claude opus", cfg)[0]["executor"] == "claude-opus"
    assert briefing.parse("1 agy", cfg)[0]["executor"] == "antigravity"
    assert briefing.parse("1 gemini", cfg)[0]["executor"] == "antigravity"
    assert briefing.parse("ore 2.5", cfg) == [{"cmd": "hours", "hours": 2.5}]


@pytest.mark.parametrize("text", ["ciao", "2 gpt", "ore 4, ok", "ore 0", "", "1"])
def test_parse_rejects(cfg, text):
    with pytest.raises(briefing.ReplyError):
        briefing.parse(text, cfg)


def test_render_escapes_and_marks():
    b = {
        "date": "2026-10-12", "status": "sent", "available_hours": 3.0, "summary": "Giornata <crash>",
        "items": [
            {"position": 1, "external_id": "acme/my-app#936", "title": "Crash <AppHome>",
             "url": "https://github.com/x?a=1&b=2", "proposed_executor": "claude-sonnet", "final_executor": None,
             "decision_source": "project_rule", "proposed_type": "bugfix", "estimate_hours": 1.0,
             "rationale": "r", "status": "proposed"},
            {"position": 2, "external_id": "PROJ-1", "title": "t", "url": "u", "proposed_executor": "claude-sonnet",
             "final_executor": "me", "decision_source": "telegram", "proposed_type": "feature",
             "estimate_hours": 0.5, "rationale": None, "status": "proposed"},
            {"position": 3, "external_id": "PROJ-2", "title": "s", "url": "u", "proposed_executor": "me",
             "final_executor": None, "decision_source": "planner", "proposed_type": "feature",
             "estimate_hours": 2, "rationale": None, "status": "skipped"},
        ],
    }
    text = briefing.render(b)
    assert "Briefing lun 12/10" in text and "Giornata &lt;crash&gt;" in text
    assert '<a href="https://github.com/x?a=1&amp;b=2">my-app#936</a> Crash &lt;AppHome&gt;' in text
    assert "claude-sonnet 📌 · bugfix · 1h" in text and "io ✏️ · feature · 0.5h" in text
    assert "<s><b>3.</b>" in text and "Totale 1.5h su 3h" in text
    assert "<code>ok</code>" in text


@pytest.fixture
def server(tmp_path, cfg):
    def fake(system, message, schema, model, timeout):
        ids = [t["task_id"] for t in json.loads(message.split("<tasks>")[1].split("</tasks>")[0])]
        items = [{"task_id": i, "type": "bugfix", "executor": "claude-haiku", "estimate_hours": 1, "rationale": "r"}
                 for i in ids]
        return {"summary": f"{len(items)} task", "items": items}

    app = App(cfg, str(tmp_path / "b.db"), token="secret", runner=fake, telegram=TG)
    tasks = [
        {"source": "github", "external_id": f"alex-dev/novainfo#{n}", "container": "alex-dev/novainfo",
         "title": f"t{n}", "url": "u", "source_status": "OPEN", "labels": [], "created_by": "alex-dev"}
        for n in (1, 2)
    ]
    ingest(app.conn, cfg, {"tasks": tasks}, "2026-10-01T08:00:00+00:00")
    srv = app.server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Authorization": "Bearer secret"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def msg(body, message_id, **over):
    return {"chat_id": TG["chat_id"], "thread_id": TG["thread_id"], "from_id": TG["owner_id"],
            "message_id": message_id, "text": body, **over}


def test_briefing_flow(server):
    app, url = server
    plan = post(f"{url}/plan", {})
    assert plan["is_briefing"] and plan["message"]["chat_id"] == TG["chat_id"]
    assert plan["message"]["thread_id"] == 16 and "<b>2.</b>" in plan["message"]["text"]
    assert post(f"{url}/briefing/sent", {"date": plan["date"], "message_id": 500}) == {"ok": True}
    assert post(f"{url}/plan", {"if_missing": True})["message"] is None  # già inviato: niente doppio briefing

    first = post(f"{url}/reply", msg("1 sonnet", 500))
    assert first["message"]["text"].startswith("✏️ 1 → claude-sonnet. Non ancora confermato")
    r = post(f"{url}/reply", msg("2 io, ok", 501))
    assert not r["is_briefing"] and r["message"]["reply_to_message_id"] == 501
    assert r["message"]["text"].startswith("✅ 2 → me, confermato.")
    b = planner.briefing(app.conn, app.today())
    assert b["status"] == "confirmed"
    assert [(i["final_executor"], i["status"], i["decision_source"]) for i in b["items"]] == [
        ("claude-sonnet", "approved", "telegram"), ("me", "approved", "telegram"),
    ]
    assert post(f"{url}/reply", msg("2 io, ok", 501)).get("duplicate")  # retry del webhook
    edit = post(f"{url}/reply", msg("1 opus", 499))
    assert edit["message"]["text"].startswith("✅ 1 → claude-opus.")  # briefing già confermato

    bad = post(f"{url}/reply", msg("7 haiku", 502))
    assert "non c'è la voce 7" in bad["message"]["text"]
    replies = app.conn.execute("SELECT count(*) FROM briefing_replies").fetchone()[0]
    assert replies == 4

    again = post(f"{url}/reply", msg("ore 5", 503))
    assert again["is_briefing"] and again["message"]["reply_to_message_id"] is None
    b = planner.briefing(app.conn, app.today())
    assert (b["status"], b["available_hours"]) == ("draft", 5)


@pytest.mark.parametrize("over", [{"from_id": 7}, {"thread_id": 1}, {"chat_id": -1}, {"text": None}])
def test_reply_ignores_others(server, over):
    _, url = server
    post(f"{url}/plan", {})
    r = post(f"{url}/reply", msg("ok", 600, **over))
    assert r == {"message": None, "is_briefing": False, "ignored": True}


def test_old_briefings_expire(server):
    app, url = server
    app.conn.execute(
        "INSERT INTO briefings (date, available_hours, status, planner_executor, created_at) "
        "VALUES ('2020-01-01', 3, 'sent', 'claude-sonnet', 'x')"
    )
    post(f"{url}/plan", {})
    assert planner.briefing(app.conn, "2020-01-01")["status"] == "expired"


def test_parse_added_tasks(cfg):
    add = lambda source, ref, executor: {"cmd": "add", "source": source, "ref": ref, "executor": executor}  # noqa: E731
    assert briefing.parse("https://github.com/alex-dev/novainfo/issues/2 agy", cfg) == [
        add("github", "alex-dev/novainfo#2", "antigravity")]
    assert briefing.parse("fai PROJ-412 con opus", cfg) == [add("jira", "PROJ-412", "claude-opus")]
    assert briefing.parse("https://example.atlassian.net/browse/PROJ-9", cfg) == [add("jira", "PROJ-9", None)]
    assert briefing.parse("salta tutto, novainfo#2 io ok", cfg) == [
        {"cmd": "skip_all"}, add("github", "novainfo#2", "me"), {"cmd": "confirm"}]
    with pytest.raises(briefing.ReplyError, match="esecutore sconosciuto"):
        briefing.parse("novainfo#2 nella board release", cfg)  # non è un comando del briefing: va all'interprete


def test_replace_briefing_with_linked_task(server):
    app, url = server
    post(f"{url}/plan", {})
    r = post(f"{url}/reply", msg("salta tutto, https://github.com/alex-dev/novainfo/issues/2 agy, ok", 700))
    assert r["message"]["text"].startswith("✅ tutte le voci saltate, 2 novainfo#2 → antigravity, confermato.")
    b = planner.briefing(app.conn, app.today())
    assert [(i["external_id"], i["final_executor"], i["status"]) for i in b["items"]] == [
        ("alex-dev/novainfo#1", None, "skipped"), ("alex-dev/novainfo#2", "antigravity", "approved"),
    ]
    missing = post(f"{url}/reply", msg("PROJ-999 agy", 701))
    assert "PROJ-999 non è tra i task importati" in missing["message"]["text"]

    # task importato ma non nel briefing: nuova voce in coda
    app.conn.execute("DELETE FROM briefing_items WHERE position = 2")
    r = post(f"{url}/reply", msg("novainfo#2", 702))
    assert "2 novainfo#2 → claude-sonnet" in r["message"]["text"]  # senza esecutore: routing (feature)
    item = planner.briefing(app.conn, app.today())["items"][-1]
    assert (item["position"], item["final_executor"], item["status"]) == (2, "claude-sonnet", "approved")


def test_help_and_show_briefing(server):
    app, url = server
    first = post(f"{url}/reply", msg("briefing", 800))  # nessun briefing: lo genera adesso
    assert first["is_briefing"] and "<b>Briefing" in first["message"]["text"]
    help_text = post(f"{url}/reply", msg("Aiuto", 801))["message"]["text"]
    assert help_text.startswith("🧠 <b>Comandi Jarvis</b>") and "<code>briefing</code>" in help_text
    post(f"{url}/plan", {})
    post(f"{url}/reply", msg("1 io", 802))
    shown = post(f"{url}/reply", msg("Briefing", 803))
    assert not shown["is_briefing"] and shown["message"]["reply_to_message_id"] == 803
    assert "<b>1.</b>" in shown["message"]["text"] and "io ✏️" in shown["message"]["text"]
    assert "Nessun ingest jira registrato" in shown["message"]["text"]  # avvisi sotto il titolo


def test_planner_failure_goes_to_telegram(server):
    app, url = server
    calls = []
    original = app.runner

    def failing(*args):
        calls.append(1)
        if len(calls) == 1:
            raise planner.PlannerError("claude -p error: Invalid API key · Please run /login")
        return original(*args)

    app.runner = failing
    r = post(f"{url}/plan", {"if_missing": True})
    assert not r["is_briefing"] and r["error"].startswith("claude -p error")
    assert r["message"]["text"].startswith("⚠️ <b>Briefing non generato</b>")
    assert "claude setup-token" in r["message"]["text"]
    assert "Nessun briefing valido per oggi. Il planner di stamattina è fallito" in post(
        f"{url}/reply", msg("ok", 899))["message"]["text"]
    shown = post(f"{url}/reply", msg("briefing", 900))  # dopo un fallimento "briefing" riprova, non rimostra l'errore
    assert shown["is_briefing"] and planner.briefing(app.conn, app.today())["status"] == "draft"
    again = post(f"{url}/reply", msg("ore 3", 901))  # rigenera anche dopo un fallimento
    assert again["is_briefing"] and planner.briefing(app.conn, app.today())["status"] == "draft"


def test_planner_quota_hint():
    from jarvis import checks
    hint = checks.planner_hint("claude -p success: You've hit your session limit · resets 12:30pm (Europe/Rome)")
    assert "Quota di Claude esaurita" in hint and "<code>briefing</code>" in hint
