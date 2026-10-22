import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from jarvis import config, i18n, planner, runs
from jarvis.ingest import ingest
from jarvis.server import App

CONFIG = Path(__file__).resolve().parent / "fixtures" / "projects.yaml"
TG = {"chat_id": -100123, "thread_id": 16, "owner_id": 42}


class Notifier:
    def __init__(self):
        self.sent = []

    def send(self, text, thread_id=-1, reply_to=None):
        self.sent.append(text)
        self.last = (thread_id, reply_to)
        return True


@pytest.fixture
def server(tmp_path):
    cfg = config.load(CONFIG)

    def fake(system, message, schema, model, timeout):
        ids = [t["task_id"] for t in json.loads(message.split("<tasks>")[1].split("</tasks>")[0])]
        executors = ["claude-sonnet", "me", "antigravity"]
        return {"summary": "s", "items": [
            {"task_id": i, "type": "feature", "executor": executors[n % 3], "estimate_hours": 1, "rationale": "r"}
            for n, i in enumerate(sorted(ids))]}

    notifier = Notifier()
    app = App(cfg, str(tmp_path / "r.db"), token="secret", runner=fake, telegram=TG, runner_token="rt",
              notifier=notifier)
    tasks = [{"source": "github", "external_id": f"alex-dev/novainfo#{n}", "container": "alex-dev/novainfo",
              "title": f"t{n}", "description": f"d{n}", "url": f"https://github.com/alex-dev/novainfo/issues/{n}",
              "source_status": "OPEN", "labels": [], "created_by": "alex-dev"} for n in (1, 2, 3)]
    ingest(app.conn, cfg, {"tasks": tasks}, "2026-10-01T08:00:00+00:00")
    srv = app.server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, notifier, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def post(url, body, token="secret"):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"http": e.code, **json.load(e)}


def say(url, text, message_id):
    body = {"chat_id": TG["chat_id"], "thread_id": TG["thread_id"], "from_id": TG["owner_id"],
            "message_id": message_id, "text": text}
    return post(f"{url}/reply", body)["message"]["text"]


def test_parse_go():
    assert runs.parse_go("vai 1") == {"positions": [1], "executor": None}
    assert runs.parse_go("Vai 3, 1 e 2")["positions"] == [1, 2, 3]
    assert runs.parse_go("lancia tutti") == {"positions": [], "executor": None}
    assert runs.parse_go("vai 4 agy") == {"positions": [4], "executor": "agy"}
    assert runs.parse_go("vai 4 con claude opus")["executor"] == "claude opus"
    assert runs.parse_go("vai") is None and runs.parse_go("1 sonnet") is None


def test_parse_go_base():
    assert runs.parse_go("vai 4 base:redesign") == {"positions": [4], "executor": None, "base": "redesign"}
    # il nome del branch resta com'è scritto, il resto del comando no
    assert runs.parse_go("Vai 4 agy base=Feature/X-1") == {"positions": [4], "executor": "agy", "base": "Feature/X-1"}
    assert runs.parse_go("vai tutti base: redesign")["base"] == "redesign"
    assert "base" not in runs.parse_go("vai 4 agy")
    assert runs.split_base("vai 4") == ("vai 4", None)
    assert runs.valid_base("redesign") and runs.valid_base("release/1.2-x") and runs.valid_base("upgrade/rn-0.86")
    assert not any(runs.valid_base(b) for b in ("-x", "--upload-pack=x", "a..b", "x/", "x.lock", "a b", ""))


def test_parse_go_note():
    assert runs.split_note("vai 4") == ("vai 4", None)
    assert runs.split_note("vai 4 nota: escludi i rimborsi") == ("vai 4", "escludi i rimborsi")
    assert runs.split_note("vai 4 note: exclude refunds") == ("vai 4", "exclude refunds")
    assert runs.split_note("vai 4 nota: prima riga\nseconda riga") == ("vai 4", "prima riga\nseconda riga")
    assert runs.parse_go("vai 4 nota: escludi rimborsi") == {"positions": [4], "executor": None, "note": "escludi rimborsi"}
    assert runs.parse_go("vai 4 nota: riga 1\nriga 2") == {"positions": [4], "executor": None, "note": "riga 1\nriga 2"}
    assert runs.parse_go("vai 4 agy base:develop note: v2 API") == {
        "positions": [4], "executor": "agy", "base": "develop", "note": "v2 API"
    }


def test_go_with_note_passes_user_note_to_runner(server):
    app, notifier, url = server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    say(url, "vai 1 nota: gestire solo rimborsi positivi", 2)
    row = app.conn.execute("SELECT owner_note, plan_note FROM runs WHERE id = 1").fetchone()
    assert row["owner_note"] == "gestire solo rimborsi positivi" and row["plan_note"] is None
    job = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert job["user_note"] == "gestire solo rimborsi positivi"


def test_note_propagates_through_approve_and_revise(server):
    app, notifier, url = server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    say(url, "vai 1 piano nota: priorità a backward compat", 2)

    # 1. Il plan run riceve owner_note in user_note
    job1 = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert job1["kind"] == "plan"
    assert job1["user_note"] == "priorità a backward compat"
    assert "note" not in job1["plan"]  # non è una revisione

    # Simula completamento del piano
    post(f"{url}/runner/update", {"run_id": 1, "status": "done", "step": "agent", "plan": "Piano bozza"}, token="rt")

    # 2. Revisione: modifica 1 <testo>
    say(url, "modifica 1 aggiungi gestione errori", 3)
    row2 = app.conn.execute("SELECT owner_note, plan_note FROM runs WHERE id = 2").fetchone()
    assert row2["owner_note"] == "priorità a backward compat"
    assert row2["plan_note"] == "aggiungi gestione errori"

    job2 = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert job2["user_note"] == "priorità a backward compat"
    assert job2["plan"]["note"] == "aggiungi gestione errori"  # nessun raddoppio tra owner_note e plan_note!

    post(f"{url}/runner/update", {"run_id": 2, "status": "done", "step": "agent", "plan": "Piano v2"}, token="rt")

    # 3. Approvazione: approva 2 -> parte il run agent con owner_note preservato
    say(url, "approva 2", 4)
    row3 = app.conn.execute("SELECT owner_note, plan_of FROM runs WHERE id = 3").fetchone()
    assert row3["owner_note"] == "priorità a backward compat"
    assert row3["plan_of"] == 2

    job3 = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert job3["kind"] == "agent"
    assert job3["user_note"] == "priorità a backward compat"
    assert job3["approved_plan"] == "Piano v2"


def test_note_propagates_on_quota_retry(server):
    app, notifier, url = server
    app.cfg.raw["fallbacks"] = {"claude-sonnet": ["antigravity"]}
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    say(url, "vai 1 nota: attenzione ai timeout", 2)
    job = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert job["user_note"] == "attenzione ai timeout"

    # Simula rate limit failure
    post(f"{url}/runner/update", {"run_id": 1, "status": "failed", "step": "agent", "rate_limited": True,
                                  "error": "rate limit"}, token="rt")
    row2 = app.conn.execute("SELECT id, executor, owner_note FROM runs WHERE parent_run_id = 1").fetchone()
    assert row2 is not None and row2["owner_note"] == "attenzione ai timeout"
    job2 = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert job2["user_note"] == "attenzione ai timeout"


def test_needs_info_notification_strict_matching(server):
    app, notifier, url = server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    say(url, "vai 1", 2)
    post(f"{url}/runner/claim", {}, token="rt")

    # 1. Fallimento agente con messaggio esatto no_change e summary -> notif.needs_info con hint comandi
    post(f"{url}/runner/update", {
        "run_id": 1, "status": "failed", "step": "agent",
        "error": i18n.messages("it")["runner.err.no_change"],
        "summary": "Quale formato di risposta dobbiamo supportare?"
    }, token="rt")
    sent = notifier.sent[-1]
    assert "❓ Run #1" in sent
    assert "Quale formato di risposta dobbiamo supportare?" in sent
    assert "vai 1 nota: &lt;chiarimenti&gt;" in sent

    # 2. Errore git contenente 'no changes' ma non errore no_change -> normale notif.failed
    say(url, "vai 1", 3)
    post(f"{url}/runner/claim", {}, token="rt")
    post(f"{url}/runner/update", {
        "run_id": 2, "status": "failed", "step": "agent",
        "error": "git commit failed: no changes added to commit (use 'git add')",
        "summary": "tentativo fallito"
    }, token="rt")
    sent2 = notifier.sent[-1]
    assert "❌ Run #2" in sent2
    assert "❓ Run #2" not in sent2

    # 3. Senza briefing item associato: notif.needs_info non include il suggerimento comandi con posizione errata
    app.conn.execute("UPDATE runs SET briefing_item_id = NULL WHERE id = 1")
    app.conn.execute("UPDATE runs SET notified_status = NULL WHERE id = 1")
    run1 = app.conn.execute("SELECT * FROM runs WHERE id = 1").fetchone()
    notif_text = runs.notification(app.conn, dict(run1))
    assert "❓ Run #1" in notif_text
    assert "vai 1 nota:" not in notif_text


def test_base_branch_override_default_and_repair(server):
    app, notifier, url = server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    assert "non valido" in say(url, "vai 1 base:-x", 2)
    queued = say(url, "vai 1 base:redesign", 3)
    assert queued.startswith("🚀 In coda: 1 novainfo#1 → claude-sonnet (base redesign)")
    job = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert job["base_branch"] == "redesign"
    post(f"{url}/runner/update", {"run_id": 1, "status": "pr_open", "step": "pr", "tests": "passed",
                                 "branch": "agent/1", "pr_url": "https://github.com/alex-dev/novainfo/pull/9"},
         token="rt")
    assert "riprendo le PR del run #1" in say(url, "ripara 1", 4)  # la riparazione eredita la base
    job = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert (job["kind"], job["base_branch"]) == ("repair", "redesign")
    post(f"{url}/runner/update", {"run_id": 2, "status": "done", "step": "pr"}, token="rt")

    # senza override vale base_branch del progetto, e viene salvata sul run
    app.cfg.raw["projects"]["novainfo"]["base_branch"] = "develop"
    say(url, "vai 3 haiku", 5)
    job = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert job["base_branch"] == "develop"
    assert app.conn.execute("SELECT base_branch FROM runs WHERE id = ?", (job["run_id"],)).fetchone()[0] == "develop"


def test_repair_of_run_without_base_keeps_the_default_base(server):
    app, notifier, url = server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    say(url, "vai 1", 2)
    assert post(f"{url}/runner/claim", {}, token="rt")["run"]["base_branch"] is None
    post(f"{url}/runner/update", {"run_id": 1, "status": "pr_open", "step": "pr", "tests": "passed",
                                 "branch": "agent/1", "pr_url": "https://github.com/alex-dev/novainfo/pull/9"},
         token="rt")
    app.cfg.raw["projects"]["novainfo"]["base_branch"] = "develop"  # configurato dopo l'apertura della PR
    assert "riprendo le PR del run #1" in say(url, "ripara 1", 3)
    job = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert (job["kind"], job["base_branch"]) == ("repair", None)  # la PR sta su main: non cambia base


def test_full_run_flow(server):
    app, notifier, url = server
    post(f"{url}/plan", {})
    assert "non approvata" in say(url, "vai 1", 1)  # briefing non ancora confermato
    say(url, "ok", 2)
    assert "la fai tu" in say(url, "vai 2", 3)
    queued = say(url, "vai tutti", 4)
    assert queued.startswith("🚀 In coda: 1 novainfo#1 → claude-sonnet, 3 novainfo#3 → antigravity")
    assert "runner mai visto" in queued
    assert "già in coda" in say(url, "vai 1", 5)

    # il runner usa solo il suo token
    assert post(f"{url}/runner/claim", {}, token="secret")["http"] == 401
    job = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert (job["branch"], job["executor"]["cli"], job["task"]["description"]) == ("agent/1", "claude", "d1")
    assert job["task"]["type"] == "feature"  # tipo scelto dal planner
    assert job["project"]["name"] == "novainfo" and job["project"]["test"] and job["limits"]["stale_hours"] == 3
    assert post(f"{url}/runner/claim", {}, token="rt")["run"] is None  # uno alla volta
    assert notifier.sent[-1].startswith("▶️ Run #1")

    post(f"{url}/runner/update", {"run_id": 1, "step": "agent"}, token="rt")
    assert "#1" in say(url, "coda", 6) and "agent" in say(url, "coda", 7)
    r = post(f"{url}/runner/update", {"run_id": 1, "status": "pr_open", "step": "pr", "tests": "passed",
                                     "pr_url": "https://github.com/alex-dev/novainfo/pull/9"}, token="rt")
    assert r == {"ok": True, "status": "pr_open", "cancel": False}
    assert "PR draft: https://github.com/alex-dev/novainfo/pull/9" in notifier.sent[-1]
    assert post(f"{url}/runner/update", {"run_id": 1, "status": "failed"}, token="rt")["http"] == 400

    job = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert job["executor"]["cli"] == "agy"
    post(f"{url}/runner/update", {"run_id": 2, "status": "failed", "step": "agent", "error": "quota",
                                 "rate_limited": True}, token="rt")
    assert "Quota di antigravity esaurita: riparte con claude-sonnet (run #3)" in notifier.sent[-1]
    job = post(f"{url}/runner/claim", {}, token="rt")["run"]  # il fallback con la controparte
    assert (job["run_id"], job["executor"]["name"], job["fallback_from"], job["reviewer"]["name"]) == (
        3, "claude-sonnet", "antigravity", "antigravity")
    post(f"{url}/runner/update", {"run_id": 3, "status": "failed", "step": "agent", "error": "quota",
                                 "rate_limited": True, "quota": [{"cli": "claude", "kind": "rate_limited"}]},
         token="rt")
    assert "Quota dell'esecutore esaurita" in notifier.sent[-1]  # mai un fallback del fallback
    assert app.conn.execute("SELECT COUNT(*) FROM quota_events WHERE run_id = 3").fetchone()[0] == 1
    post(f"{url}/runner/claim", {}, token="rt")  # niente in coda: nessun effetto
    items = {i["position"]: i["status"] for i in planner.briefing(app.conn, app.today())["items"]}
    assert items == {1: "done", 2: "approved", 3: "approved"}  # la 3 fallita si può rilanciare
    assert "esecutore sconosciuto" in say(url, "vai 3 gpt", 10)
    assert say(url, "vai 3 haiku", 8).startswith("🚀 In coda: 3 novainfo#3 → claude-haiku")
    item = planner.briefing(app.conn, app.today())["items"][2]
    assert (item["final_executor"], item["decision_source"], item["status"]) == ("claude-haiku", "telegram", "queued")
    assert "Tolti dalla coda: 1" in say(url, "svuota coda", 9)


def test_stale_run_fails(server):
    app, notifier, url = server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    say(url, "vai 1", 2)
    post(f"{url}/runner/claim", {}, token="rt")
    app.conn.execute("UPDATE runs SET heartbeat_at = '2020-01-01T00:00:00+00:00'")
    post(f"{url}/runner/claim", {}, token="rt")
    assert app.conn.execute("SELECT status FROM runs WHERE id = 1").fetchone()[0] == "failed"
    assert "nessuna notizia dal runner" in notifier.sent[-1]


def test_runner_routes_disabled_without_token(tmp_path):
    app = App(config.load(CONFIG), str(tmp_path / "x.db"), token="secret")
    srv = app.server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert post(f"http://127.0.0.1:{srv.server_address[1]}/runner/claim", {}, token="")["http"] == 503
    finally:
        srv.shutdown()


def test_command_in_wrong_topic_gets_a_hint(server):
    app, notifier, url = server
    body = {"chat_id": TG["chat_id"], "thread_id": None, "from_id": TG["owner_id"], "message_id": 50}
    assert post(f"{url}/reply", {**body, "text": "3 agy ok"})["ignored"]
    assert "topic 🧠 Jarvis" in notifier.sent[-1] and notifier.last == (None, 50)  # risposta in General
    post(f"{url}/reply", {**body, "message_id": 51, "text": "coda"})
    assert len(notifier.sent) == 2
    post(f"{url}/reply", {**body, "message_id": 52, "text": "ciao a tutti"})  # chiacchiere: nessun avviso
    post(f"{url}/reply", {**body, "message_id": 53, "from_id": 7, "text": "coda"})  # altri utenti: niente
    assert len(notifier.sent) == 2


def test_failed_run_sends_agent_summary(server):
    app, notifier, url = server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    say(url, "vai 1", 2)
    post(f"{url}/runner/claim", {}, token="rt")
    post(f"{url}/runner/update", {"run_id": 1, "status": "failed", "step": "agent",
                                 "error": "nessuna modifica: l'agente si è fermato",
                                 "summary": "Il fix è nel submodule <framework>."}, token="rt")
    assert "<b>Riepilogo dell'agente</b>\nIl fix è nel submodule &lt;framework&gt;." in notifier.sent[-1]


def test_go_with_ticket_adds_and_queues(server):
    app, notifier, url = server
    ingest(app.conn, config.load(CONFIG), {"tasks": [
        {"source": "github", "external_id": "acme/my-lib#12",
         "container": "acme/my-lib", "title": "Process death Tripo", "url": "u12",
         "source_status": "OPEN", "labels": [], "created_by": "alex-dev"}]}, "2026-10-13T08:00:00+00:00")
    post(f"{url}/plan", {})  # briefing non confermato: la voce aggiunta parte lo stesso
    r = say(url, "vai my-lib#12 sonnet", 1)
    assert r.startswith("🚀 In coda: 4 my-lib#12 → claude-sonnet")
    assert "già in coda" in say(url, "vai my-lib#12 opus", 2)
    assert "non è tra i task importati" in say(url, "vai my-lib#99 sonnet", 3)
    assert "esecutore sconosciuto" in say(url, "vai my-lib#12 gpt", 4)
    job = post(f"{url}/runner/claim", {}, token="rt")["run"]
    # il task del framework gira nel worktree dell'app, con il submodule come focus
    assert (job["task"]["external_id"], job["project"]["name"], job["focus"]) == (
        "acme/my-lib#12", "my-app", "my-lib")
    assert job["repo"] == "acme/my-app"
    assert job["submodules"][0] | {"local_path": "-"} == {
        "path": "my-lib", "project": "my-lib", "local_path": "-",
        "repo": "acme/my-lib", "test": "./gradlew formatKotlin testDebugUnitTest",
        "copy_files": ["local.properties"]}
    assert any("changelog.md" in n for n in job["project"]["agent_notes"])


def test_stop_queued_and_running(server):
    app, notifier, url = server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    say(url, "vai 1 3", 2)
    assert "⚠️ Runner offline (mai visto)" in say(url, "coda", 3)
    post(f"{url}/runner/claim", {}, token="rt")  # run 1 in corso, run 2 in coda
    assert "Runner offline" not in say(url, "coda", 4)
    assert say(url, "ferma 2", 5) == "⏹ Run #2 tolto dalla coda."
    assert "il runner lo interrompe" in say(url, "ferma", 6)  # senza numero: quello in corso
    assert post(f"{url}/runner/update", {"run_id": 1}, token="rt")["cancel"] is True
    post(f"{url}/runner/update", {"run_id": 1, "status": "cancelled", "step": "agent"}, token="rt")
    assert notifier.sent[-1].startswith("⏹ Run #1")
    assert "non è in corso" in say(url, "ferma 1", 7)
    items = {i["position"]: i["status"] for i in planner.briefing(app.conn, app.today())["items"]}
    assert items[1] == items[3] == "approved"


def test_pr_on_submodule_and_app(server):
    app, notifier, url = server
    ingest(app.conn, app.cfg, {"tasks": [
        {"source": "github", "external_id": "acme/my-lib#12",
         "container": "acme/my-lib", "title": "Process death", "url": "u12",
         "source_status": "OPEN", "labels": [], "created_by": "alex-dev"}]}, "2026-10-13T08:00:00+00:00")
    post(f"{url}/plan", {})
    say(url, "vai my-lib#12 sonnet", 1)
    assert "bump" not in say(url, "aiuto", 2)
    post(f"{url}/runner/claim", {}, token="rt")
    post(f"{url}/runner/update", {"run_id": 1, "status": "pr_open", "pr_url": "https://github.com/a/pull/7",
                                  "sub_pr_urls": ["https://github.com/f/pull/5"]}, token="rt")
    sent = notifier.sent[-1]
    assert sent.index("PR submodule: https://github.com/f/pull/5") < sent.index("PR app: https://github.com/a/pull/7")
    assert "prima il submodule" in sent
    assert app.conn.execute("SELECT project FROM runs WHERE id = 1").fetchone()[0] == "my-app"


def test_claude_with_exhausted_quota_starts_with_the_counterpart(server):
    app, notifier, url = server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    app.conn.execute("""INSERT INTO quota_events (cli, kind, reset_at, observed_at)
                        VALUES ('claude', 'rate_limited', '2999-01-01T00:00:00+00:00', '2026-10-16T08:00:00+00:00')""")
    say(url, "vai 1", 2)
    job = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert (job["executor"]["name"], job["fallback_from"]) == ("antigravity", "claude-sonnet")
    assert "Quota di claude-sonnet ancora esaurita" in notifier.sent[-1]
    item = planner.briefing(app.conn, app.today())["items"][0]
    assert (item["final_executor"], item["decision_source"]) == ("antigravity", "quota_fallback")
    post(f"{url}/runner/update", {"run_id": 1, "status": "failed", "step": "agent", "error": "quota",
                                  "rate_limited": True}, token="rt")
    assert "Quota dell'esecutore esaurita" in notifier.sent[-1]  # niente rimbalzo verso Claude
    assert app.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1

    # reset passato (o rate limit senza orario più vecchio di un'ora): Claude torna disponibile
    app.conn.execute("UPDATE quota_events SET reset_at = '2026-01-01T00:00:00+00:00'")
    assert runs.claude_exhausted(app.conn) is None
    app.conn.execute("UPDATE quota_events SET reset_at = NULL, observed_at = '2026-01-01T00:00:00+00:00'")
    assert runs.claude_exhausted(app.conn) is None


# --- fallback ordinato (#12) ---

def _vai_1(server, chain, quota_cli=None):
    app, notifier, url = server
    app.cfg.raw["fallbacks"] = {"claude-sonnet": chain}
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    if quota_cli:
        app.conn.execute("""INSERT INTO quota_events (cli, kind, reset_at, observed_at)
                            VALUES (?, 'rate_limited', '2999-01-01T00:00:00+00:00', '2026-10-16T08:00:00+00:00')""",
                         (quota_cli,))
    say(url, "vai 1", 2)
    return app, notifier, url


def _fail(url, run_id):
    post(f"{url}/runner/update", {"run_id": run_id, "status": "failed", "step": "agent", "error": "quota",
                                  "rate_limited": True}, token="rt")


def test_fallback_walks_the_chain_in_order_and_stops_at_the_end(server):
    app, notifier, url = _vai_1(server, ["antigravity", "claude-haiku"])
    order = []
    for run_id in (1, 2, 3):
        job = post(f"{url}/runner/claim", {}, token="rt")["run"]
        order.append(job["executor"]["name"])
        _fail(url, run_id)
    assert order == ["claude-sonnet", "antigravity", "claude-haiku"]
    assert post(f"{url}/runner/claim", {}, token="rt")["run"] is None  # catena finita: nessun altro tentativo
    assert app.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3
    assert "Quota dell'esecutore esaurita" in notifier.sent[-1]


def test_exhausted_provider_is_skipped_at_the_start_including_other_executors_of_it(server):
    app, notifier, url = _vai_1(server, ["claude-haiku", "antigravity"], quota_cli="claude")
    job = post(f"{url}/runner/claim", {}, token="rt")["run"]
    # claude-haiku usa la stessa CLI di claude-sonnet, con la quota esaurita: si salta ad antigravity
    assert (job["executor"]["name"], job["fallback_from"]) == ("antigravity", "claude-sonnet")
    _fail(url, 1)
    assert post(f"{url}/runner/claim", {}, token="rt")["run"] is None  # haiku resta da evitare


def test_fallback_chain_resolution_and_reviewer(tmp_path):
    cfg = config.load(CONFIG)
    assert cfg.fallback_chain("claude-sonnet") == ["antigravity"]  # counterparts di sempre
    assert cfg.fallback_chain("antigravity") == ["claude-sonnet"]
    assert cfg.counterpart("claude-opus") == "antigravity" and cfg.counterpart("me") is None
    cfg.raw["fallbacks"] = {"anthropic": ["claude-haiku", "antigravity"], "claude-opus": ["antigravity"]}
    assert cfg.fallback_chain("claude-sonnet") == ["claude-haiku", "antigravity"]  # chiave di famiglia
    assert cfg.fallback_chain("claude-opus") == ["antigravity"]  # l'esecutore batte la famiglia
    assert cfg.fallback_chain("claude-haiku") == ["antigravity"]  # mai se stesso
    assert cfg.counterpart("claude-sonnet") == "antigravity"  # revisore: il primo di un'altra famiglia


def test_fallbacks_are_validated(tmp_path):
    import yaml
    raw = yaml.safe_load(CONFIG.read_text())
    for bad in ({"nope": ["antigravity"]}, {"claude-sonnet": ["me"]}, {"claude-sonnet": ["antigravity", "antigravity"]},
                {"claude-sonnet": ["claude-sonnet"]}, {"claude-sonnet": "antigravity"}):
        path = tmp_path / "f.yaml"
        path.write_text(yaml.safe_dump({**raw, "fallbacks": bad}))
        with pytest.raises(config.ConfigError):
            config.load(path)
    path.write_text(yaml.safe_dump({**raw, "fallbacks": {"anthropic": ["antigravity"]}}))
    assert config.load(path).fallback_chain("claude-haiku") == ["antigravity"]


def test_quota_events_accept_any_cli_after_migration(server):
    app, _, _ = server
    app.conn.execute("INSERT INTO quota_events (cli, kind, observed_at) VALUES ('copilot', 'rate_limited', 'x')")
    assert app.conn.execute("SELECT cli FROM quota_events").fetchone()[0] == "copilot"
