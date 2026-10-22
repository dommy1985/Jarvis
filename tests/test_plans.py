from jarvis import runs
from tests.test_runs import post, say, server  # noqa: F401 - fixture condivisa

PLAN = "1. Cambiare TaxiHomeViewModel.findTaxis\n2. Aggiornare il test <TaxiHomeViewModelTest>"


def claim(url):
    return post(f"{url}/runner/claim", {}, token="rt")["run"]


def finish_plan(url, run_id, plan=PLAN):
    post(f"{url}/runner/update", {"run_id": run_id, "status": "done", "step": "agent", "plan": plan}, token="rt")


def test_parse_plan_token():
    assert runs.parse_go("vai 4 piano") == {"positions": [4], "executor": None, "plan": True}
    assert runs.parse_go("vai 4 piano agy base:redesign") == {
        "positions": [4], "executor": "agy", "base": "redesign", "plan": True}
    assert runs.parse_go("vai tutti piano sonnet")["plan"] is True
    assert runs.parse_go("vai 4 agy") == {"positions": [4], "executor": "agy"}
    assert runs.split_plan("vai x#1 piano sonnet") == ("vai x#1 sonnet", True)
    assert runs.split_plan("vai x#1 pianoforte") == ("vai x#1 pianoforte", False)
    assert runs.APPROVE.fullmatch("approva") and runs.APPROVE.fullmatch("approva piano 12").group(1) == "12"
    assert runs.REVISE.fullmatch("modifica 12 aggiungi un test").groups() == ("12", "aggiungi un test")
    assert runs.REVISE.fullmatch("modifica la issue").groups() == (None, "la issue")


def test_plan_flow_revise_approve(server):  # noqa: F811
    app, notifier, url = server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    r = say(url, "vai 1 piano", 2)
    assert "(prima il piano)" in r and "approva N" in r
    job = claim(url)
    assert job["kind"] == "plan" and job["plan"] == {"timeout_minutes": 15} and "reviewer" not in job
    assert "prepara il piano" in notifier.sent[-1] and "sola lettura" in notifier.sent[-1]

    finish_plan(url, job["run_id"])
    sent = notifier.sent[-1]
    assert sent.startswith("📝 <b>Piano</b> · run #1") and "&lt;TaxiHomeViewModelTest&gt;" in sent
    assert "<code>approva 1</code>" in sent and "<code>modifica 1 &lt;testo&gt;</code>" in sent
    item = app.conn.execute("SELECT status FROM briefing_items WHERE id = (SELECT briefing_item_id FROM runs WHERE id = 1)")
    assert item.fetchone()[0] == "queued"  # in attesa di Davide: la voce non torna libera
    assert "vai 1" not in say(url, "vai 1", 3) or "già in coda" in say(url, "vai 1", 4)
    assert "📝 #1" in say(url, "coda", 5) and "approva 1" in say(url, "coda", 6)

    assert say(url, "modifica 1 aggiungi anche il changelog", 7).startswith("✏️ In coda un nuovo piano (run #2) dal #1")
    assert "già rivisto" in say(url, "approva 1", 8)
    job2 = claim(url)
    assert job2["kind"] == "plan" and job2["plan"]["previous"] == PLAN
    assert job2["plan"]["note"] == "aggiungi anche il changelog"
    assert "rivede il piano" in notifier.sent[-1]
    finish_plan(url, 2, plan=PLAN + "\n3. Changelog")
    assert "· rivisto" in notifier.sent[-1] and "3. Changelog" in notifier.sent[-1]

    assert say(url, "approva", 9).startswith("👍 Piano #2 approvato: in coda il lavoro con claude-sonnet (run #3)")
    job3 = claim(url)
    assert job3["kind"] == "agent" and job3["approved_plan"].endswith("3. Changelog") and "plan" not in job3
    assert "Segue il piano #2" in notifier.sent[-1]
    assert "già approvato" in say(url, "approva 2", 10)


def test_plan_notification_highlights_questions(server):  # noqa: F811
    app, notifier, url = server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    say(url, "vai 1 piano", 2)
    job = claim(url)
    plan_with_q = (
        "1. Causa: fix calcolo\n"
        "2. Modifiche: app/Foo.kt\n"
        "3. Test: FooTest.kt\n"
        "4. **Rischi e domande**:\n"
        "- Come gestire i rimborsi parziali?\n"
    )
    finish_plan(url, job["run_id"], plan=plan_with_q)
    sent = notifier.sent[-1]
    assert "❓ <b>Domande e decisioni aperte:</b>" in sent
    assert "• Come gestire i rimborsi parziali?" in sent
    assert "<code>modifica 1 &lt;tua risposta&gt;</code>" in sent


def test_discard_and_errors(server):  # noqa: F811
    app, notifier, url = server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    assert "nessun piano in attesa" in say(url, "approva", 2)
    assert "non è un piano" in say(url, "approva 5", 3)
    say(url, "vai 1 piano", 4)
    job = claim(url)
    assert "non ancora pronto" in say(url, "scarta 1", 5)
    finish_plan(url, job["run_id"])
    assert say(url, "scarta", 6).startswith("🗑 Piano #1 scartato")
    items = {i["position"]: i["status"] for i in __import__("jarvis.planner", fromlist=["x"]).briefing(
        app.conn, app.today())["items"]}
    assert items[1] == "approved"  # si può rilanciare
    # "modifica ..." senza numero e senza piani in attesa non è un comando sul piano
    assert not say(url, "modifica la issue 4", 7).startswith("✏️")


def test_plan_first_project(server):  # noqa: F811
    app, notifier, url = server
    app.cfg.projects["novainfo"]["plan_first"] = True
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    assert "(prima il piano)" in say(url, "vai 1", 2)
    assert claim(url)["kind"] == "plan"


def test_log_snapshot_and_follow(server, monkeypatch):  # noqa: F811
    app, notifier, url = server
    edits = []
    notifier.send = lambda text, **kw: notifier.sent.append(text) or 77
    notifier.edit = lambda mid, text: edits.append((mid, text)) or True
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    say(url, "vai 1", 2)
    assert "nessun run in corso" in say(url, "log", 3)
    job = claim(url)
    assert "Nessuna riga di log" in say(url, "log", 4)
    post(f"{url}/runner/update", {"run_id": job["run_id"], "step": "agent",
                                  "log": ["🔧 Read <A>.kt", "✏️ Edit B.kt", ""]}, token="rt")
    snap = say(url, "log", 5)
    assert "Run #1" in snap and "agent" in snap and "🔧 Read &lt;A&gt;.kt" in snap and "✏️ Edit B.kt" in snap

    reply = post(f"{url}/reply", {"chat_id": -100123, "thread_id": 16, "from_id": 42, "message_id": 6, "text": "segui"})
    assert reply["message"] is None and app.following[1]["message_id"] == 77
    assert "basta" in notifier.sent[-1]
    app.follow_tick()
    assert edits == []  # niente di nuovo: nessuna modifica
    post(f"{url}/runner/update", {"run_id": 1, "log": ["$ ./gradlew test"]}, token="rt")
    app.follow_tick()
    assert edits[-1][0] == 77 and "$ ./gradlew test" in edits[-1][1]
    post(f"{url}/runner/update", {"run_id": 1, "status": "failed", "error": "boh"}, token="rt")
    app.follow_tick()
    assert "Run finito" in edits[-1][1] and not app.following
    assert "Il run non è in corso" in say(url, "segui", 7)

    assert say(url, "basta", 8) == "Non stai seguendo nessun run."
    for _ in range(4):  # tetto di righe per run (100 per invio)
        runs.add_log(app.conn, 1, ["x"] * 100)
    assert app.conn.execute("SELECT COUNT(*) FROM run_log WHERE run_id = 1").fetchone()[0] == runs.LOG_PER_RUN
