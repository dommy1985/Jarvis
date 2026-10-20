import json
import subprocess

import pytest

from jarvis import providers
from jarvis.providers.base import Provider, ProviderError, run_structured
from jarvis.providers.agy import AgyProvider
from jarvis.providers.claude import ClaudeProvider

SCHEMA = {"type": "object"}
claude = ClaudeProvider()


def test_registry_maps_cli_names_to_providers():
    assert providers.get("claude") is claude or isinstance(providers.get("claude"), ClaudeProvider)
    assert providers.get("nope") is None
    assert isinstance(providers.get("claude"), Provider)
    assert {"execute", "plan_structured", "review"} <= providers.get("claude").capabilities
    assert "quota" not in providers.get("claude").capabilities  # Claude non espone le quote
    assert isinstance(providers.get("agy"), AgyProvider) and "quota" in providers.get("agy").capabilities
    assert providers.can("agy", "plan_structured") and providers.can("claude", "plan_structured")
    assert not providers.can("nope", "execute")


def test_claude_exec_command_allows_project_commands_and_blocks_push():
    cmd = claude.build_exec_cmd({"model": "sonnet"}, "p", skip_permissions=False, timeout_minutes=60,
                                commands=["./gradlew test && yarn lint", None, "uv run pytest"])
    assert cmd[:6] == ["claude", "-p", "p", "--model", "sonnet", "--output-format"]
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert allowed[:2] == ["Agent", "Task"] and {"Bash(./gradlew:*)", "Bash(yarn lint:*)", "Bash(uv run:*)"} <= set(allowed)
    assert cmd[cmd.index("--disallowedTools") + 1] == "Bash(git push:*),Bash(gh:*)"
    assert cmd[cmd.index("--setting-sources") + 1] == "project,local"


def test_claude_plan_and_review_commands_are_read_only():
    plan = claude.build_plan_cmd({"model": "opus"}, "p", timeout_minutes=15)
    assert plan[plan.index("--tools") + 1] == "Read,Grep,Glob" and "--no-session-persistence" in plan
    assert "--permission-mode" not in plan
    review = claude.build_review_cmd({"model": "haiku"}, "p", schema=SCHEMA, timeout_minutes=15)
    assert review[review.index("--json-schema") + 1] == json.dumps(SCHEMA)
    assert review[review.index("--tools") + 1] == "Read,Grep,Glob"
    assert review[review.index("--output-format") + 1] == "json"


def test_claude_structured_command_has_no_tools_or_settings():
    cmd = claude.build_structured_cmd("sonnet", "sys", "msg", SCHEMA)
    assert cmd[:3] == ["claude", "-p", "msg"] and cmd[cmd.index("--system-prompt") + 1] == "sys"
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--setting-sources") + 1] == ""
    assert {"--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"} <= set(cmd)


def test_claude_parse_structured():
    ok = json.dumps({"structured_output": {"a": 1}})
    assert claude.parse_structured(0, ok, "") == {"a": 1}
    with pytest.raises(ProviderError, match="exit 1: boom"):
        claude.parse_structured(1, "not json", "boom")
    with pytest.raises(ProviderError, match="claude -p error_max_turns"):
        claude.parse_structured(0, json.dumps({"is_error": True, "subtype": "error_max_turns", "result": "x"}), "")


def test_claude_parse_output_and_review():
    assert claude.parse_output(124, "")[1] == "agente oltre il tempo massimo"
    done = json.dumps({"type": "result", "result": "Fatto."})
    assert claude.parse_output(0, done) == ("Fatto.", None, False)
    assert claude.parse_output(1, "Claude AI usage limit reached|1760000000\n")[2] is True
    assert claude.parse_review(124, "")[1] == "revisore oltre il tempo massimo"
    review = {"verdict": "ok", "summary": "s", "issues": []}
    assert claude.parse_review(0, json.dumps({"structured_output": review})) == (review, None, False)
    assert claude.parse_review(0, json.dumps({"result": "x"}))[1] == "il revisore non ha restituito la review strutturata"
    assert claude.quota_reset("limit reached|1760000000") == "2025-10-09T08:53:20+00:00"


def test_check_ready_reports_a_missing_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert claude.check_ready() == "claude non trovato nel PATH"
    monkeypatch.setattr("shutil.which", lambda name: "/bin/claude")
    assert claude.check_ready() is None


def test_run_structured_runs_in_a_temp_dir_and_maps_timeouts(monkeypatch):
    seen = {}

    def fake_run(cmd, cwd, capture_output, text, timeout):
        seen.update(cmd=cmd, cwd=cwd, timeout=timeout)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"structured_output": {"ok": True}}), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_structured(claude, "sys", "msg", SCHEMA, "sonnet", 30) == {"ok": True}
    assert seen["timeout"] == 30 and seen["cmd"][0] == "claude" and "jarvis" not in seen["cwd"]

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ProviderError, match="claude -p oltre 5s"):
        run_structured(claude, "s", "m", SCHEMA, "sonnet", 5)


agy = AgyProvider()


def test_agy_commands_follow_the_current_flags():
    ex = agy.build_exec_cmd({"model": "gemini-3.8-flash-high"}, "p", commands=[], skip_permissions=False, timeout_minutes=60)
    assert ex == ["agy", "-p", "p", "--model", "gemini-3.8-flash-high", "--mode", "accept-edits",
                  "--output-format", "stream-json", "--print-timeout", "60m"]
    skip = agy.build_exec_cmd({"model": "m"}, "p", commands=[], skip_permissions=True, timeout_minutes=5)
    assert skip[5] == "--dangerously-skip-permissions" and "--mode" not in skip
    plan = agy.build_plan_cmd({"model": "m"}, "p", timeout_minutes=15)
    assert plan[plan.index("--mode") + 1] == "plan" and "--sandbox" in plan and plan[-1] == "15m"
    review = agy.build_review_cmd({"model": "m"}, "p", schema=SCHEMA, timeout_minutes=15)
    assert "--sandbox" in review and review[review.index("--json-schema") + 1] == json.dumps(SCHEMA)
    assert review[review.index("--output-format") + 1] == "json" and review[-1] == "15m"


def test_agy_allows_read_only_git_in_the_given_submodules():
    allowed = agy.allowed_commands([], ["lib"])
    assert "git status" in allowed and "git -C lib grep" in allowed and "git -C lib ls-files" in allowed
    assert not [c for c in agy.allowed_commands([], []) if c.startswith("git -C")]
    assert "Bash" not in " ".join(claude.allowed_commands(["./gradlew test"], []))
    assert "./gradlew" in claude.allowed_commands(["./gradlew test"], [])


def test_agy_parse_output(tmp_path, monkeypatch):
    assert agy.parse_output(124, "")[1] == "agente oltre il tempo massimo"
    ok = json.dumps({"status": "SUCCESS", "response": "Fatto"})
    assert agy.parse_output(0, ok) == ("Fatto", None, False)
    summary, error, limited = agy.parse_output(3, 'AGY_ERROR: {"code": "RESOURCE_EXHAUSTED"}')
    assert error.startswith("agy: ") and limited is True
    monkeypatch.setattr("jarvis.providers.agy.AGY_CONVERSATIONS", tmp_path)
    (tmp_path / "c1.db").write_bytes(b'x"CommandLine":"rm -rf /tmp/x"y')
    denied = json.dumps({"response": "Ok", "conversation_id": "c1", "denied_actions": [{"display_name": "run_command"}]})
    summary, error, _ = agy.parse_output(0, denied)
    assert error is None and "run_command `rm -rf /tmp/x`" in summary
    assert agy.parse_output(0, json.dumps({"response": "Ok", "conversation_id": "../etc", "denied_actions": [{}]}))[0].endswith("?)")


def test_agy_parse_review():
    review = {"verdict": "ok", "summary": "s", "issues": []}
    assert agy.parse_review(0, json.dumps({"structured_output": review})) == (review, None, False)
    assert agy.parse_review(124, "")[1] == "revisore oltre il tempo massimo"
    assert agy.parse_review(3, 'AGY_ERROR: {"code": "QUOTA"}')[2] is True
    assert agy.parse_review(0, "not json")[1].startswith("output del revisore non leggibile")
    assert agy.parse_review(0, json.dumps({"is_error": True, "subtype": "x", "result": "quota"}))[2] is True


def test_agy_parse_structured():
    assert agy.parse_structured(0, json.dumps({"structured_output": {"a": 1}}), "") == {"a": 1}
    assert agy.parse_structured(0, json.dumps({"response": '{"a": 2}'}), "") == {"a": 2}
    with pytest.raises(ProviderError, match="agy -p: "):
        agy.parse_structured(3, "", 'AGY_ERROR: {"code": "X"}')
    with pytest.raises(ProviderError, match="exit 1"):
        agy.parse_structured(1, "", "boom")
    with pytest.raises(ProviderError):
        agy.parse_structured(0, json.dumps({"status": "SUCCESS", "response": "plain text"}), "")
    cmd = agy.build_structured_cmd("m", "sys", "msg", SCHEMA)
    assert cmd[2] == "sys\n\nmsg" and "--json-schema" in cmd and "--sandbox" in cmd


def test_agy_quota_snapshot_and_exhaustion():
    usage = json.dumps({"command": {"data": {"groups": [{"buckets": [
        {"id": "gemini-5h", "remaining_fraction": 0.0, "reset_time": "2026-10-20T20:00:00Z"},
        {"id": "3p-5h", "remaining_fraction": 0.4, "reset_time": "2026-10-20T21:00:00Z"}]}]}}})
    seen = []
    buckets = agy.usage(lambda cmd, cwd, timeout: seen.append(cmd) or (0, usage))
    assert seen[0][:4] == ["agy", "-p", "/usage", "--output-format"]
    assert [b["bucket"] for b in buckets] == ["gemini-5h", "3p-5h"] and buckets[0]["kind"] == "snapshot"
    assert [b["bucket"] for b in agy.exhausted("gemini-3.8-flash-high", buckets)] == ["gemini-5h"]
    assert agy.exhausted("claude-sonnet-4-6", buckets) == []
    assert agy.usage(lambda *a: (1, "garbage")) == []
    assert claude.usage(lambda *a: (0, "")) == [] and claude.exhausted("sonnet", buckets) == []


def test_event_lines_per_provider():
    assert claude.event_lines({"type": "assistant", "message": {"content": [{"type": "text", "text": "ciao"}]}}) == ["💬 ciao"]
    step = {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE", "tool_name": "run_command",
                                                   "tool_info": {"parameters": {"CommandLine": "ls"}}}}
    assert agy.event_lines(step) == ["$ ls"]
    assert claude.uses_task_suffix is True and agy.uses_task_suffix is False


# --- structured output: nativo, oppure schema nel prompt + validazione + un retry ---

from jarvis.providers.schema import validate
from jarvis.providers.structured import call_structured

OBJ = {"type": "object", "additionalProperties": False, "required": ["n", "kind"],
       "properties": {"n": {"type": "integer", "minimum": 0}, "kind": {"type": "string", "enum": ["a", "b"]},
                      "tags": {"type": "array", "items": {"type": "string"}}, "note": {"type": ["string", "null"]}}}


class FakeProvider(Provider):
    """Provider finto: restituisce le risposte in sequenza e registra i prompt ricevuti."""
    name, binary = "fake", "fake"

    def __init__(self, answers, native=False):
        self.answers, self.prompts = list(answers), []
        self.capabilities = frozenset({"plan_structured"} if native else ())

    def build_structured_cmd(self, model, system, message, schema):
        self.prompts.append(message)
        return ["fake"]

    def parse_structured(self, returncode, stdout, stderr):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    build_exec_cmd = build_plan_cmd = build_review_cmd = parse_output = parse_review = None
    allowed_commands = lambda self, commands, submodule_paths: []  # noqa: E731


def fake_run(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))


def test_validate_reports_each_problem_with_its_path():
    assert validate({"n": 1, "kind": "a", "tags": ["x"], "note": None}, OBJ) == []
    errors = validate({"n": -1, "kind": "z", "tags": [1], "extra": 1}, OBJ)
    assert any("minimo" in e for e in errors) and any("valori ammessi" in e for e in errors)
    assert any("$.tags[0]" in e for e in errors) and any("campo non previsto extra" in e for e in errors)
    assert validate({"kind": "a"}, OBJ) == ["$: manca n"]
    assert validate([], OBJ)[0].startswith("$: atteso object")
    assert validate(True, {"type": "integer"})  # bool non è un intero


def test_native_provider_gets_no_schema_in_the_prompt(monkeypatch):
    fake_run(monkeypatch)
    provider = FakeProvider([{"n": 1, "kind": "a"}], native=True)
    assert call_structured(provider, "sys", "msg", OBJ, "m", 5) == {"n": 1, "kind": "a"}  # lo schema lo applica la CLI
    assert provider.prompts == ["msg"]


def test_non_native_provider_gets_the_schema_in_the_prompt(monkeypatch):
    fake_run(monkeypatch)
    provider = FakeProvider([{"n": 1, "kind": "a"}])
    assert call_structured(provider, "sys", "msg", OBJ, "m", 5) == {"n": 1, "kind": "a"}
    assert len(provider.prompts) == 1 and provider.prompts[0].startswith("msg") and '"required"' in provider.prompts[0]


def test_invalid_answer_is_retried_once_with_the_errors(monkeypatch):
    fake_run(monkeypatch)
    provider = FakeProvider([{"n": "x", "kind": "a"}, {"n": 2, "kind": "b"}])
    assert call_structured(provider, "sys", "msg", OBJ, "m", 60) == {"n": 2, "kind": "b"}
    assert len(provider.prompts) == 2 and "$.n" in provider.prompts[1] and "non era valida" in provider.prompts[1]


def test_second_invalid_answer_fails_clearly(monkeypatch):
    fake_run(monkeypatch)
    provider = FakeProvider([{"n": "x", "kind": "a"}, {"kind": "q"}])
    with pytest.raises(ProviderError, match="fake: risposta non conforme.*secondo tentativo"):
        call_structured(provider, "sys", "msg", OBJ, "m", 60)
    assert len(provider.prompts) == 2


def test_provider_errors_are_not_retried(monkeypatch):
    fake_run(monkeypatch)
    provider = FakeProvider([ProviderError("fake: no auth"), {"n": 1, "kind": "a"}])
    with pytest.raises(ProviderError, match="no auth"):
        call_structured(provider, "sys", "msg", OBJ, "m", 5)
    assert len(provider.prompts) == 1


def test_runner_for_uses_the_configured_provider_and_maps_errors(monkeypatch):
    from jarvis import config, planner
    from pathlib import Path
    cfg = config.load(Path(__file__).resolve().parent / "fixtures" / "projects.yaml")
    fake = FakeProvider([ProviderError("fake: boom")])
    monkeypatch.setattr(providers, "get", lambda cli: fake)
    fake_run(monkeypatch)
    with pytest.raises(planner.PlannerError, match="fake: boom"):
        planner.runner_for(cfg, "planner")("s", "m", OBJ, "model", 5)


def test_config_accepts_any_registered_cli_for_planner_and_commands(tmp_path):
    import yaml
    from jarvis import config
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).resolve().parent / "fixtures" / "projects.yaml").read_text())
    raw["planner"]["executor"] = "antigravity"
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert config.load(path).planner["executor"] == "antigravity"
    raw["executors"]["antigravity"]["cli"] = "nope"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(config.ConfigError, match="cli registrata"):
        config.load(path)
    raw["planner"]["executor"] = "me"  # esecutore umano: nessuna cli
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(config.ConfigError):
        config.load(path)


def test_native_output_is_validated_without_retry(monkeypatch):
    fake_run(monkeypatch)
    provider = FakeProvider([{"n": "x", "kind": "a"}], native=True)
    with pytest.raises(ProviderError, match="non conforme allo schema: .*\\$\\.n"):
        call_structured(provider, "sys", "msg", OBJ, "m", 5)
    assert len(provider.prompts) == 1


def test_retry_shares_one_time_budget(monkeypatch):
    import jarvis.providers.structured as structured
    fake_run(monkeypatch)
    timeouts = []
    monkeypatch.setattr(structured, "run_structured",
                        lambda provider, system, message, schema, model, timeout: timeouts.append(timeout) or {"n": "x"})
    clock = iter([0, 0, 100, 100, 100])  # deadline, primo giro, secondo giro...
    monkeypatch.setattr(structured.time, "monotonic", lambda: next(clock))
    with pytest.raises(ProviderError, match="anche al secondo tentativo"):
        call_structured(FakeProvider([]), "s", "m", OBJ, "model", 150)
    assert timeouts == [150, 50]  # il retry riceve solo il tempo che resta
    clock = iter([0, 0, 140, 140])
    timeouts.clear()
    monkeypatch.setattr(structured.time, "monotonic", lambda: next(clock))
    with pytest.raises(ProviderError) as e:
        call_structured(FakeProvider([]), "s", "m", OBJ, "model", 150)
    assert timeouts == [150] and "secondo tentativo" not in str(e.value)  # restano 10s: niente retry


def test_example_config_for_the_open_source_repo_loads():
    from pathlib import Path
    from jarvis import config
    root = Path(__file__).resolve().parent.parent
    cfg = config.load(next(p for p in (root / "config" / "projects.example.yaml",
                                       root / "oss-template" / "config" / "projects.example.yaml") if p.exists()))
    assert cfg.owner_name == "Alex" and cfg.fallback_chain("claude-sonnet") == ["antigravity"]
    assert cfg.counterpart("antigravity") == "claude-sonnet" and cfg.submodule_parent("my-shared") == ("my-service", "libs/shared")
