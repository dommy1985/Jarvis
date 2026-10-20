import json

import pytest

from jarvis import briefing, planner
from jarvis.briefing import ReplyError
from tests.test_briefing import server, msg, post  # noqa: F401 - fixture e helper condivisi
from tests.test_planner import FakeClaude, conn, item, task_id, cfg  # noqa: F401


def test_resolve_projects(cfg):
    assert briefing.resolve_projects("novainfo", cfg) == ["novainfo"]
    assert briefing.resolve_projects("Novainfo e my-app", cfg) == ["novainfo", "my-app"]
    assert briefing.resolve_projects("my-app, novainfo + OrbitNext", cfg) == [
        "my-app", "novainfo", "orbit-next"]
    assert briefing.resolve_projects("portalpa", cfg) == ["portalpa-sito"]   # prefisso univoco
    assert briefing.resolve_projects("OrbitClassic", cfg) == ["orbit-classic"]  # nome del repo
    assert briefing.resolve_projects("proj", cfg) == ["my-app"]  # chiave Jira
    assert briefing.resolve_projects("tutti", cfg) == [] and briefing.resolve_projects("novainfo e tutti", cfg) == []
    with pytest.raises(ReplyError, match="ambiguo"):
        briefing.resolve_projects("orbit", cfg)  # orbit-classic, orbit-next, orbit-be
    with pytest.raises(ReplyError, match="sconosciuto"):
        briefing.resolve_projects("pippo", cfg)
    with pytest.raises(ReplyError, match="in pausa"):
        briefing.resolve_projects("catch-ball", cfg)
    assert cfg.focus_projects(["my-app"]) == {"my-app", "my-lib"}  # con il submodule
    assert cfg.focus_projects([]) is None and cfg.focus_projects(["novainfo"]) == {"novainfo"}


def test_parse_focus_commands(cfg):
    assert briefing.parse("briefing su novainfo", cfg) == [
        {"cmd": "hours", "hours": None, "focus": ["novainfo"], "set_focus": True}]
    assert briefing.parse("Ore 3 su my-app, novainfo", cfg) == [  # la virgola non spezza il comando
        {"cmd": "hours", "hours": 3.0, "focus": ["my-app", "novainfo"], "set_focus": True}]
    for text in ("briefing su my-app  ore 5", "briefing su my-app, 5 ore", "ore 5 su my-app",
                 "Briefing su my-app 5ore"):
        assert briefing.parse(text, cfg) == [
            {"cmd": "hours", "hours": 5.0, "focus": ["my-app"], "set_focus": True}], text
    assert briefing.parse("briefing su novainfo e my-app ore 2,5", cfg)[0]["focus"] == [
        "novainfo", "my-app"]
    with pytest.raises(ReplyError, match="due volte"):
        briefing.parse("ore 3 su novainfo ore 4", cfg)
    with pytest.raises(ReplyError, match="fuori scala"):
        briefing.parse("briefing su novainfo ore 30", cfg)
    assert briefing.parse("briefing su tutti", cfg)[0]["focus"] == []
    assert briefing.parse("ore 4", cfg) == [{"cmd": "hours", "hours": 4.0}]  # senza "su": nessun cambio di focus
    with pytest.raises(ReplyError):
        briefing.parse("ore 20 su novainfo", cfg)
    with pytest.raises(ReplyError, match="sconosciuto"):
        briefing.parse("briefing su pippo", cfg)


def test_planner_sees_only_focus_projects(conn, cfg):
    all_refs = {t["ref"] for t in planner.candidates(conn, cfg, "2026-10-11")}
    assert {"PROJ-1", "alex-dev/novainfo#31", "acme/my-lib#9"} == all_refs
    assert {t["ref"] for t in planner.candidates(conn, cfg, "2026-10-11", ["novainfo"])} == {"alex-dev/novainfo#31"}
    # my-app porta con sé il suo submodule e i ticket Jira PROJ
    assert {t["ref"] for t in planner.candidates(conn, cfg, "2026-10-11", ["my-app"])} == {
        "PROJ-1", "acme/my-lib#9"}

    fake = FakeClaude({"summary": "Solo novainfo.", "items": [item(task_id(conn, "alex-dev/novainfo#31"))]})
    planner.plan(conn, cfg, "2026-10-12", 2, runner=fake, focus=["novainfo"])
    context = json.loads(fake.calls[0]["message"].split("\n\n<tasks>")[0])
    assert context["focus_projects"] == ["novainfo"] and context["available_hours"] == 2
    assert "focus_projects" in fake.calls[0]["system"]
    b = planner.briefing(conn, "2026-10-12")
    assert b["focus"] == ["novainfo"] and "🎯 novainfo" in briefing.render(b)
    # rigenerato senza focus: torna a tutti
    planner.plan(conn, cfg, "2026-10-12", 2, force=True, runner=FakeClaude({"summary": "x", "items": []}))
    assert planner.briefing(conn, "2026-10-12")["focus"] is None
    # nessun task nel focus: briefing vuoto con la spiegazione, senza chiamare Claude
    empty = FakeClaude({"summary": "mai", "items": []})
    planner.plan(conn, cfg, "2026-10-13", 2, runner=empty, focus=["orbit-next"])
    assert empty.calls == [] and "in orbit-next" in planner.briefing(conn, "2026-10-13")["summary"]


def test_focus_commands_from_telegram(server):  # noqa: F811
    app, url = server
    seen = []
    original = app.runner

    def spy(system, message, schema, model, timeout):
        seen.append(json.loads(message.split("\n\n<tasks>")[0]).get("focus_projects"))
        return original(system, message, schema, model, timeout)

    app.runner = spy
    post(f"{url}/plan", {})
    r = post(f"{url}/reply", msg("ore 2 su novainfo", 700))
    assert r["is_briefing"] and "🎯 novainfo" in r["message"]["text"] and "2h disponibili" in r["message"]["text"]
    assert planner.briefing(app.conn, app.today())["focus"] == ["novainfo"]
    again = post(f"{url}/reply", msg("ore 4", 701))  # solo le ore: il focus di oggi resta
    assert "🎯 novainfo" in again["message"]["text"] and "4h disponibili" in again["message"]["text"]
    other = post(f"{url}/reply", msg("briefing su my-app e novainfo", 702))  # cambia focus, tiene le ore
    assert "🎯 my-app, novainfo" in other["message"]["text"] and "4h disponibili" in other["message"]["text"]
    cleared = post(f"{url}/reply", msg("briefing su tutti", 703))
    assert "🎯" not in cleared["message"]["text"]
    assert seen[-4:] == [["novainfo"], ["novainfo"], ["my-app", "my-lib", "novainfo"], None]
    bad = post(f"{url}/reply", msg("briefing su pippo", 704))  # non finisce all'interprete GitHub
    assert "progetto sconosciuto" in bad["message"]["text"] and not bad["is_briefing"]
    assert "briefing su other-app" in post(f"{url}/reply", msg("aiuto", 705))["message"]["text"]


def test_projects_command(server):  # noqa: F811
    app, url = server
    post(f"{url}/plan", {})
    text = post(f"{url}/reply", msg("progetti", 800))["message"]["text"]
    assert text.startswith("📂 <b>Progetti</b> · 8 attivi, 1 in pausa")
    assert "<b>novainfo</b> · expo · 2 task aperti" in text and "alex-dev/novainfo" in text
    assert "<b>my-app</b> · android" in text and "Jira PROJ" in text and "include my-lib" in text
    assert "i task girano nel worktree di my-app" in text  # my-lib
    assert "⏸ In pausa" in text and "catch-ball" in text and "🎯" not in text
    post(f"{url}/reply", msg("briefing su novainfo", 801))
    assert "🎯 Briefing di oggi concentrato su: novainfo" in post(f"{url}/reply", msg("Lista progetti", 802))["message"]["text"]
    assert "<code>progetti</code>" in post(f"{url}/reply", msg("aiuto", 803))["message"]["text"]


def test_regenerate_after_runs_exist_and_internal_errors_reach_telegram(server, monkeypatch):  # noqa: F811
    from jarvis import server as srv
    app, url = server
    post(f"{url}/plan", {})
    post(f"{url}/reply", msg("ok", 900))
    # un run già creato su una voce del briefing (come i piani di stamattina)
    item = app.conn.execute("SELECT id, task_id FROM briefing_items LIMIT 1").fetchone()
    app.conn.execute("""INSERT INTO runs (task_id, briefing_item_id, executor, model, status, queued_at, kind)
                        VALUES (?, ?, 'claude-sonnet', 'sonnet', 'queued', '2026-10-19T08:00:00+00:00', 'plan')""",
                     (item["task_id"], item["id"]))
    r = post(f"{url}/reply", msg("briefing su novainfo ore 5", 901))
    assert r["is_briefing"] and "🎯 novainfo" in r["message"]["text"] and "5h disponibili" in r["message"]["text"]
    assert app.conn.execute("SELECT briefing_item_id FROM runs").fetchone()[0] is None  # run conservato, scollegato
    assert post(f"{url}/reply", msg("ore 4", 902))["is_briefing"]  # e di nuovo, senza errori

    monkeypatch.setattr(srv.Handler, "_regenerate", lambda self, *a: 1 / 0)
    boom = post(f"{url}/reply", msg("ore 3", 903))
    assert "Errore interno di Jarvis: ZeroDivisionError" in boom["message"]["text"] and not boom["is_briefing"]
