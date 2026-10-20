import copy
import json
import re
import string
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from jarvis import briefing, commands, config, i18n, outcomes, runs
from jarvis.ingest import ingest
from jarvis.providers import structured
from jarvis.server import App
from runner import runner

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "tests" / "fixtures" / "projects.yaml"
TG = {"chat_id": -100123, "thread_id": 16, "owner_id": 42}
LOCALES = i18n.available()


def placeholders(text: str) -> set[str]:
    return {name.split(".")[0].split("[")[0] for _, name, _, _ in string.Formatter().parse(text) if name is not None}


# --- catalogo ---

def test_both_builtin_locales_are_installed():
    assert {"en", "it"} <= set(LOCALES) and i18n.DEFAULT == "en"


def test_message_keys_are_the_same_in_every_locale():
    keys = {code: set(i18n.messages(code)) for code in LOCALES}
    for code, found in keys.items():
        assert found == keys["en"], (code, sorted(found ^ keys["en"]))


@pytest.mark.parametrize("code", [c for c in LOCALES if c != "en"])
def test_placeholders_match_the_english_messages(code):
    english, other = i18n.messages("en"), i18n.messages(code)
    for key, text in english.items():
        assert isinstance(other[key], str) and other[key].strip(), (code, key)
        assert placeholders(other[key]) == placeholders(text), (code, key)


def test_every_message_is_a_valid_format_string():
    for code in LOCALES:
        for key, text in i18n.messages(code).items():
            params = {name: "x" for name in placeholders(text)}
            if params:
                text.format(**{k: 1 if ":g" in text else "x" for k in params})  # non solleva


def test_vocabulary_keys_match_and_words_are_lowercase_lists():
    keys = {code: set(i18n.vocabulary(code)) for code in LOCALES}
    for code, found in keys.items():
        assert found == keys["en"], (code, sorted(found ^ keys["en"]))
        for key, words in i18n.vocabulary(code).items():
            assert isinstance(words, list) and words and all(isinstance(w, str) and w for w in words), (code, key)
            if key != "weekdays":
                assert all(w == w.lower() for w in words), (code, key)
    assert all(len(i18n.vocabulary(c)["weekdays"]) == 7 for c in LOCALES)


def test_prompt_files_exist_for_every_locale_with_the_same_placeholders():
    names = {code: {p.name for p in (ROOT / "prompts" / code).glob("*.md")} for code in LOCALES}
    assert names["en"] >= {"task.md", "task-suffix.md", "review.md", "fix.md", "plan.md", "plan-revise.md",
                           "approved-plan.md", "planner.md", "commands.md"}
    for code in LOCALES:
        assert names[code] == names["en"], code
        for name in names["en"]:
            tokens = lambda c: set(re.findall(r"\{[a-z_]+\}", i18n.prompt(name.removesuffix(".md"), c)))  # noqa: E731
            assert tokens(code) == tokens("en"), (code, name)


def test_italian_prompts_are_the_original_ones():
    # i prompt it sono i file storici di prompts/, con i segnaposto {owner}/{owner_desc}
    assert i18n.prompt("planner", "it").startswith("Sei il planner di Jarvis")
    assert "{owner_desc}" in i18n.prompt("planner", "it") and "{owner_desc}" in i18n.prompt("planner", "en")


def test_runner_markers_survive_translation():
    for code in LOCALES:
        messages = i18n.messages(code)
        assert messages["runner.commit_body"].startswith(outcomes.RUNNER_MARK)  # outcomes.changed_by_others
        assert messages["runner.realign_commit_body"].count(outcomes.RUNNER_MARK) == 1
        assert messages["runner.sub_pr_link"].startswith(messages["marker.submodule_comment"])
        assert messages["runner.repair.head"].startswith("{marker}")
    # il filtro dei commenti di Jarvis riconosce quelli di ogni locale
    assert set(outcomes.jarvis_comments()) >= {i18n.messages(c)["marker.repair_comment"] for c in LOCALES}


def test_missing_key_falls_back_to_english(monkeypatch):
    i18n.set_locale("it")
    monkeypatch.setitem(i18n.messages("en"), "test.only_en", "only {x}")
    assert i18n.t("test.only_en", x=1) == "only 1"


def test_unknown_locale_is_rejected():
    with pytest.raises(i18n.LocaleError):
        i18n.set_locale("xx")


# --- config ---

def test_owner_locale_defaults_to_english_and_is_validated(tmp_path):
    raw = yaml.safe_load(CONFIG.read_text())
    assert config.load(CONFIG).locale == "it" and i18n.get_locale() == "it"  # il hub parla italiano
    del raw["owner"]["locale"]
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert config.load(path).locale == "en" and i18n.get_locale() == "en"
    raw["owner"]["locale"] = "klingon"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(config.ConfigError, match="owner.locale"):
        config.load(path)


# --- comandi in inglese ---

def test_english_parse_go_and_plan_words():
    with i18n.using("en"):
        assert runs.parse_go("go 1") == {"positions": [1], "executor": None}
        assert runs.parse_go("Go 3, 1 and 2")["positions"] == [1, 2, 3]
        assert runs.parse_go("launch all") == {"positions": [], "executor": None}
        assert runs.parse_go("go 4 with claude opus")["executor"] == "claude opus"
        assert runs.parse_go("go 4 plan agy") == {"positions": [4], "executor": "agy", "plan": True}
        assert runs.parse_go("go 4 base:redesign") == {"positions": [4], "executor": None, "base": "redesign"}
        assert runs.parse_go("go") is None and runs.parse_go("1 sonnet") is None
        assert runs.APPROVE.fullmatch("approve plan 12").group(1) == "12"
        assert runs.DISCARD.fullmatch("discard").group(1) is None
        assert runs.REVISE.fullmatch("revise 12 add a test").groups() == ("12", "add a test")
        assert runs.LOG.fullmatch("follow run #7").group(1) == "7"
        assert runs.REPAIR.fullmatch("repair 7 agy").groups() == ("7", "agy")
        assert runs.STOP.fullmatch("stop 3").group(1) == "3" and runs.STOP.fullmatch("stop")


def test_the_other_locale_words_are_not_understood():
    with i18n.using("en"):
        assert runs.parse_go("vai 1") is None and runs.APPROVE.fullmatch("approva 3") is None
    with i18n.using("it"):
        assert runs.parse_go("go 1") is None and runs.APPROVE.fullmatch("approve 3") is None
        assert runs.parse_go("vai 1") == {"positions": [1], "executor": None}


def test_english_briefing_replies():
    cfg = config.load(CONFIG)
    with i18n.using("en"):
        assert briefing.parse("ok", cfg) == [{"cmd": "confirm"}]
        assert briefing.parse("2 haiku, 3 skip, 4 me", cfg) == [
            {"cmd": "executor", "position": 2, "executor": "claude-haiku"}, {"cmd": "skip", "position": 3},
            {"cmd": "executor", "position": 4, "executor": "me"}]
        assert briefing.parse("hours 4.5", cfg) == [{"cmd": "hours", "hours": 4.5}]
        assert briefing.parse("skip all", cfg) == [{"cmd": "skip_all"}]
        assert briefing.parse("1 sonnet 2 me ok", cfg)[-1] == {"cmd": "confirm"}
        focus = briefing.parse("briefing on novainfo and my-app hours 3", cfg)[0]
        assert focus["hours"] == 3 and focus["focus"] == ["novainfo", "my-app"] and focus["set_focus"]
        assert briefing.parse("hours 2 for novainfo", cfg)[0]["hours"] == 2
        assert briefing.parse("briefing on all", cfg)[0]["focus"] == []
        added = briefing.parse("add novainfo#9 with agy", cfg)[0]
        assert added == {"cmd": "add", "source": "github", "ref": "novainfo#9", "executor": "antigravity"}
        for bad, message in (("ore 4", "I do not understand"), ("2 pippo", "unknown executor"),
                             ("hours 99", "out of range"), ("hours 2, ok", "must be sent alone")):
            with pytest.raises(briefing.ReplyError, match=message):
                briefing.parse(bad, cfg)


def test_english_github_confirmation_words():
    with i18n.using("en"):
        assert commands.is_yes("yes") and commands.is_no("cancel") and not commands.is_yes("sì")
        assert commands.decision("yes, create it") is True and commands.decision("no thanks") is False
        assert commands.decision("never mind") is False and commands.decision("sure") is None
    with i18n.using("it"):
        assert commands.decision("sì, crea") is True and commands.decision("lascia stare") is False


# --- risposte Telegram in inglese ---

@pytest.fixture
def english_server(tmp_path):
    cfg = config.load(CONFIG)
    cfg = config.Config(copy.deepcopy({**cfg.raw, "owner": {**cfg.raw["owner"], "locale": "en"}}))

    def fake(system, message, schema, model, timeout):
        ids = sorted(t["task_id"] for t in json.loads(message.split("<tasks>")[1].split("</tasks>")[0]))
        executors = ["claude-sonnet", "me", "antigravity"]
        return {"summary": "Summary.", "items": [
            {"task_id": i, "type": "feature", "executor": executors[n % 3], "estimate_hours": 1, "rationale": "why"}
            for n, i in enumerate(ids)]}

    sent = []

    class Notifier:
        def send(self, text, thread_id=-1, reply_to=None):
            sent.append(text)
            return len(sent)

        def edit(self, message_id, text):
            sent.append(text)
            return True

    app = App(cfg, str(tmp_path / "en.db"), token="secret", runner=fake, telegram=TG, runner_token="rt",
              notifier=Notifier())
    assert i18n.get_locale() == "en"  # App porta il locale della config nel processo
    tasks = [{"source": "github", "external_id": f"alex-dev/novainfo#{n}", "container": "alex-dev/novainfo",
              "title": f"t{n}", "description": f"d{n}", "url": f"https://github.com/alex-dev/novainfo/issues/{n}",
              "source_status": "OPEN", "labels": [], "created_by": "alex-dev"} for n in (1, 2, 3)]
    ingest(app.conn, cfg, {"tasks": tasks}, "2026-10-01T08:00:00+00:00")
    srv = app.server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, sent, f"http://127.0.0.1:{srv.server_address[1]}"
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
    return (post(f"{url}/reply", body)["message"] or {}).get("text")


def test_english_end_to_end_replies(english_server):
    app, sent, url = english_server
    assert post(f"{url}/plan", {})["is_briefing"]
    help_text = say(url, "help", 1)
    assert help_text.startswith("🧠 <b>Jarvis commands</b>") and "<code>go 1</code>" in help_text
    assert "Executors: <code>sonnet</code>, <code>opus</code>, <code>haiku</code>, <code>agy</code>, <code>me</code>" in help_text
    shown = say(url, "briefing", 2)
    assert "available #jarvis" in shown and "Total 3h of 3h" in shown and "<code>hours 4</code>" in shown
    assert re.search(r"<b>Briefing (Mon|Tue|Wed|Thu|Fri|Sat|Sun) ", shown)
    assert say(url, "go 1", 3).startswith("⚠️ item 1 is not approved: confirm the briefing with ok")
    confirmed = say(url, "ok", 4)
    assert confirmed.startswith("✅ confirmed. To start them: <code>go N</code> or <code>go all</code>.")
    assert "✅ confirmed" in confirmed
    assert say(url, "2 pippo", 5).startswith("⚠️ unknown executor: «pippo». No changes made.\nBriefing: ")
    assert say(url, "9 skip", 6).startswith("⚠️ there is no item 9. No changes made.")
    assert say(url, "go 2", 7).startswith("⚠️ item 2 is yours: assign it to an agent (2 sonnet). Nothing queued.")
    assert say(url, "go 1 base:-x", 8).startswith("⚠️ invalid base branch name: -x. Nothing queued.")
    assert say(url, "go 1 pippo", 9).startswith("⚠️ unknown executor: «pippo». Nothing queued.")
    queued = say(url, "go 1 plan", 10)
    assert queued.startswith("🚀 Queued: 1 novainfo#1 → claude-sonnet (plan first).\nThe runner is preparing the plan")
    assert "<b>Runner queue</b> · runner never seen" in queued and "plan" in queued
    assert "Runner offline (never seen): machine off or runner stopped." in queued
    assert say(url, "go 1", 11).startswith("⚠️ item 1 is already queued. Nothing queued.")
    job = post(f"{url}/runner/claim", {}, token="rt")["run"]
    assert job["locale"] == "en" and job["kind"] == "plan"  # il runner riceve la lingua del hub
    assert sent[-1].startswith("📝 Run #1: ") and "is preparing the plan (read-only: nothing is modified)" in sent[-1]
    post(f"{url}/runner/update", {"run_id": 1, "status": "done", "plan": "1. Do it"}, token="rt")
    assert "📝 <b>Plan</b> · run #1" in sent[-1] and "<code>approve 1</code> · <code>revise 1 &lt;text&gt;</code>" in sent[-1]
    assert say(url, "revise 1 add tests", 12).startswith("✏️ Queued a new plan (run #2) from #1 with your note.")
    assert say(url, "discard 1", 13).startswith("⚠️ plan #1 is already revised by a new plan.")
    assert say(url, "approve 7", 14) == "⚠️ run #7 is not a plan."
    assert say(url, "queue", 15).startswith("🏃 <b>Runner queue</b>")
    assert say(url, "clear queue", 16).startswith("⏹ Removed from the queue: 1.\n\n")
    assert say(url, "stop 99", 17) == "⚠️ run #99 does not exist."
    assert say(url, "stats", 18) == "📊 No runs in the last 30 days."
    assert say(url, "log 99", 19) == "⚠️ run #99 does not exist."
    assert say(url, "unfollow", 20) == "You are not following any run."
    projects = say(url, "projects", 21)
    assert projects.startswith("📂 <b>Projects</b> · ") and "open tasks" in projects and "no source" not in projects
    assert say(url, "hours 0", 22).startswith("⚠️ hours out of range: 0. No changes made.")


def test_english_notifications_and_status(english_server):
    app, sent, url = english_server
    post(f"{url}/plan", {})
    say(url, "ok", 1)
    say(url, "go 1", 2)
    post(f"{url}/runner/claim", {}, token="rt")
    post(f"{url}/runner/update", {"run_id": 1, "status": "pr_open", "step": "pr", "tests": "passed",
                                 "branch": "agent/1", "pr_url": "https://github.com/alex-dev/novainfo/pull/9",
                                 "review": {"reviewer": "antigravity", "issues": [{"severity": "major"}, {}],
                                            "rounds": 1}}, token="rt")
    done = sent[-1]
    assert done.startswith("✅ Run #1: ") and "    draft PR: https://github.com/alex-dev/novainfo/pull/9" in done
    assert "claude-sonnet · tests ok" in done and "review antigravity: 2 remarks, 1 serious, fixed" in done
    assert "⏳" not in done
    assert "✅ #1 " in say(url, "queue", 3) and "🏃 <b>Runner queue</b> · runner seen at " in say(url, "queue", 4)
    assert say(url, "repair 1", 5).startswith("🛠 Queued: resuming the PRs of run #1 with claude-sonnet (run #2): "
                                              "build and tests, fixing failed tests, new review.")
    assert say(url, "repair 1", 6).startswith("⚠️ a run is already queued on the PRs of run #1.")


# --- runner e provider ---

def job(**extra):
    return {
        "run_id": 7, "branch": "agent/7", "owner": "Ada",
        "executor": {"name": "claude-sonnet", "cli": "claude", "model": "sonnet", "family": "anthropic"},
        "task": {"source": "github", "external_id": "acme/app#31", "type": "bugfix", "title": "Fix crash",
                 "description": "it crashes", "url": "https://github.com/acme/app/issues/31", "labels": []},
        "project": {"name": "app", "path": "/x", "workdir": None, "setup": None, "build": "make", "test": "make test",
                    "copy_files": []},
        "limits": {}, **extra,
    }


def test_job_locale_rules():
    assert runner.job_locale({}) == "it"  # server precedente all'i18n: parlava italiano
    assert runner.job_locale({"locale": None}) == "it"
    assert runner.job_locale({"locale": "en"}) == "en" and runner.job_locale({"locale": "it"}) == "it"
    assert runner.job_locale({"locale": "fr"}) == "en"  # locale che questo runner non ha: inglese


def test_runner_prompts_follow_the_job_locale_whatever_the_process_locale():
    sub = {"path": "my-lib", "project": "my-lib", "repo": "a/my-lib", "local_path": "/x", "test": "./t"}
    with i18n.using("it"):
        english = runner.build_prompt(job(locale="en", submodules=[sub], focus="my-lib"))
        legacy = runner.build_prompt(job())
    assert "Work on this task in the current repository" in english and "Skip advisor checkpoint 3" in english
    assert "`my-lib` is a submodule (separate repo)" in english and "Submodule tests: `cd my-lib && ./t`." in english
    assert "The task belongs to the submodule `my-lib`" in english and "- Build: `make`. Test: `make test`." in english
    assert "Lavora a questo task" in legacy and "Rules:" not in legacy and "{" not in legacy.replace('{\n', '')
    with i18n.using("en"):  # anche con un processo in inglese, un job senza locale resta in italiano
        assert "Lavora a questo task" in runner.build_prompt(job())
    plan = runner.plan_prompt({**job(locale="en"), "plan": {"previous": "OLD", "note": "add changelog"}})
    assert "Prepare the PLAN for this task" in plan and "<previous_plan>\nOLD" in plan
    assert "Ada asked to change it like this" in plan


def test_runner_english_pr_text_and_feedback_keys():
    review = {"reviewer": "agy", "summary": "S", "issues": [{"severity": "major", "file": "a.py", "problem": "p",
                                                               "fix": "f"}], "rounds": 1, "fix_summary": "done"}
    body = runner.pr_body(job(locale="en", fallback_from="claude-opus"), "summary", "failed", "out", ["n"],
                          repo="acme/app", review=review)
    for text in ("Executor: `claude-sonnet` (sonnet) · Jarvis run #7", "restarted with the counterpart: `claude-opus`",
                 "Build and tests (`make && make test`): ❌ failed", "<summary>Last lines</summary>", "## Agent summary",
                 "## Cross review (agy)", "### Fixes after the review", "## Runner notes", "Closes acme/app#31",
                 "Draft PR opened by Jarvis: to be reviewed before merging."):
        assert text in body, text
    feedback = {"comments": [{"author": "bob", "pr": "u", "text": "use v3"}], "ci": [{"name": "build", "pr": "u",
                                                                                      "conclusion": "failure"}]}
    with i18n.using("en"):
        assert set(runner.pr_feedback_prompt(feedback)) == {"pr_comments", "failed_ci"}
        comment = runner.repair_comment({**job(locale="en"), "repair": {"feedback": feedback}}, "failed", "tail", [], None, "", True)
    assert comment.startswith("🛠 Jarvis repair (run #7, executor `claude-sonnet`)")
    assert "Passed to the agent: 1 review comments, failed checks: `build`." in comment
    assert "Build and tests: ❌ still failing" in comment
    assert set(runner.pr_feedback_prompt(feedback)) == {"commenti_pr", "ci_fallita"}  # processo it, job senza locale


def test_english_provider_schema_prompts():
    with i18n.using("en"):
        assert structured.t("prov.schema_prompt", schema="{}").startswith("\n\nAnswer ONLY with a valid JSON object")
        assert i18n.t("prov.retry_prompt", errors="- x").startswith("\n\nYour previous answer was not valid:")
        from jarvis.providers.schema import validate
        assert validate({"a": 1}, {"type": "object", "required": ["b"], "additionalProperties": False,
                                   "properties": {}}) == ["$: missing b", "$: unexpected field a"]
