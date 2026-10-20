import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jarvis import providers
from jarvis.providers.agy import agy_last_command
from jarvis.providers.events import agy_event_lines, claude_event_lines
from runner import runner


def run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """Repo con remote bare locale (origin/main) e un file locale da copiare nel worktree."""
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    run("git", "init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    run("git", "clone", "-q", str(remote), str(repo), cwd=tmp_path)
    for k, v in (("user.email", "d@example.com"), ("user.name", "Alex")):
        run("git", "config", k, v, cwd=repo)
    (repo / "app.txt").write_text("v1\n")
    (repo / ".gitignore").write_text("build/\n")
    run("git", "add", "-A", cwd=repo)
    run("git", "commit", "-q", "-m", "init", cwd=repo)
    run("git", "push", "-q", "origin", "main", cwd=repo)
    run("git", "remote", "set-head", "origin", "main", cwd=repo)
    (repo / "local.properties").write_text("sdk.dir=/segreto\n")  # non tracciato, non ignorato
    return repo, remote


@pytest.fixture(autouse=True)
def hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "HOOKS", tmp_path / "hooks")
    monkeypatch.setattr(runner, "LOGS", tmp_path / "logs")


class Api:
    def __init__(self, cancel_after=None):
        self.updates, self.beats, self.cancel_after = [], 0, cancel_after

    def update(self, run_id, **fields):
        self.updates.append(fields)

    def log(self, run_id, lines):
        self.logs = getattr(self, "logs", []) + list(lines)

    def heartbeat(self, run_id):
        self.beats += 1
        return self.cancel_after is not None and self.beats > self.cancel_after


def job(repo, tmp_path, **project):
    return {
        "run_id": 7, "branch": "agent/7",
        "executor": {"name": "claude-sonnet", "cli": "claude", "model": "sonnet", "family": "anthropic"},
        "task": {"source": "github", "external_id": "alex-dev/novainfo#31", "type": "bugfix",
                 "title": "Fix crash", "description": "crasha", "url": "https://github.com/x/y/issues/31", "labels": []},
        "project": {"name": "novainfo", "path": str(repo), "workdir": None, "setup": None,
                    "build": None, "test": "test -f app.txt", "copy_files": ["local.properties"], **project},
        "limits": {"worktrees": str(tmp_path / "wt"), "setup_timeout_minutes": 1, "agent_timeout_minutes": 1,
                   "test_timeout_minutes": 1, "stale_hours": 3},
    }


def fake_agent(edit=True, result="Ho corretto il crash.", is_error=False):
    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        assert env["GIT_CONFIG_VALUE_0"].endswith("hooks") and "GH_TOKEN" not in env
        assert cmd[0] == "claude" and "--permission-mode" in cmd and "<task>" in cmd[2] and "crasha" in cmd[2]
        assert (Path(cwd) / "local.properties").exists()  # file locale copiato
        if edit:
            (Path(cwd) / "app.txt").write_text("v2\n")
        return 0, json.dumps({"result": result, "is_error": is_error, "subtype": "success"})
    return agent


def fake_gh(calls):
    def gh(cmd, cwd, timeout, env=None):
        calls.append(cmd)
        return 0, "https://github.com/alex-dev/novainfo/pull/12\n"
    return gh


def push_branch(repo, name, content):
    """Branch `name` su origin con app.txt diverso da main; il repo resta su main."""
    run("git", "checkout", "-q", "-b", name, cwd=repo)
    (repo / "app.txt").write_text(content)
    run("git", "commit", "-qam", name, cwd=repo)
    run("git", "push", "-q", "origin", name, cwd=repo)
    run("git", "checkout", "-q", "main", cwd=repo)
    run("git", "fetch", "-q", cwd=repo)


def test_resolve_base(repo, tmp_path):
    repo, _ = repo
    push_branch(repo, "redesign", "redesign\n")
    assert runner.resolve_base(repo, "redesign", "worktree") == "origin/redesign"
    assert runner.resolve_base(repo, None, "worktree") == "origin/main"
    with pytest.raises(runner.StepError, match="non trovato"):
        runner.resolve_base(repo, "nope", "worktree")
    for bad in ("-x", "a..b", "x.lock"):
        with pytest.raises(runner.StepError, match="non valido"):
            runner.resolve_base(repo, bad, "worktree")


def test_execute_starts_from_base_branch_and_targets_it(repo, tmp_path):
    repo, remote = repo
    push_branch(repo, "redesign", "redesign\n")
    api, calls = Api(), []
    runner.execute({**job(repo, tmp_path), "base_branch": "redesign"}, api, run_agent=fake_agent(), gh=fake_gh(calls))
    assert api.updates[-1]["status"] == "pr_open"
    assert calls[0][calls[0].index("--base") + 1] == "redesign"  # la PR punta alla base richiesta
    parent = subprocess.run(["git", "rev-parse", "agent/7~1"], cwd=remote, capture_output=True, text=True,
                            check=True).stdout.strip()
    base = subprocess.run(["git", "rev-parse", "redesign"], cwd=remote, capture_output=True, text=True,
                          check=True).stdout.strip()
    assert parent == base  # il commit dell'agente è sopra `redesign`, non sopra main


def test_execute_fails_clearly_when_base_branch_is_missing(repo, tmp_path):
    repo, _ = repo
    api = Api()
    runner.execute({**job(repo, tmp_path), "base_branch": "nope"}, api, run_agent=fake_agent(), gh=fake_gh([]))
    final = api.updates[-1]
    assert final["status"] == "failed" and final["step"] == "worktree" and "nope" in final["error"]


def test_execute_opens_draft_pr(repo, tmp_path):
    repo, remote = repo
    api, calls = Api(), []
    runner.execute(job(repo, tmp_path), api, run_agent=fake_agent(), gh=fake_gh(calls))
    final = api.updates[-1]
    assert (final["status"], final["pr_url"], final["tests"]) == (
        "pr_open", "https://github.com/alex-dev/novainfo/pull/12", "passed")
    assert [u.get("step") for u in api.updates[:-1]] == ["worktree", "agent", "tests", "review", "push", "pr"]
    cmd = calls[0]
    assert cmd[:3] == ["gh", "pr", "create"] and "--draft" in cmd and cmd[cmd.index("--base") + 1] == "main"
    body = cmd[cmd.index("--body") + 1]
    assert "Ho corretto il crash." in body and "✅ passati" in body and "Closes alex-dev/novainfo#31" in body
    files = subprocess.run(["git", "show", "--name-only", "--format=%an|%s", "agent/7"], cwd=remote,
                           capture_output=True, text=True, check=True).stdout
    assert files.startswith("Alex|alex-dev/novainfo#31: Fix crash")
    assert "app.txt" in files and "local.properties" not in files  # il file locale non entra nel commit
    assert "Co-Authored-By" not in files
    assert not (tmp_path / "wt" / "novainfo-7").exists()  # worktree rimosso dopo la PR


def test_execute_without_changes_fails_and_keeps_worktree(repo, tmp_path):
    repo, _ = repo
    api = Api()
    runner.execute(job(repo, tmp_path), api, run_agent=fake_agent(edit=False, result="Non è chiaro cosa fare."),
                   gh=fake_gh([]))
    final = api.updates[-1]
    assert (final["status"], final["step"]) == ("failed", "agent")
    assert "nessuna modifica" in final["error"] and final["summary"] == "Non è chiaro cosa fare."
    assert (tmp_path / "wt" / "novainfo-7").exists()


def test_rate_limit_is_reported(repo, tmp_path):
    repo, _ = repo
    api = Api()
    runner.execute(job(repo, tmp_path), api, gh=fake_gh([]),
                   run_agent=fake_agent(edit=False, result="Claude usage limit reached", is_error=True))
    assert api.updates[-1]["rate_limited"] and api.updates[-1]["status"] == "failed"


def test_failed_tests_still_open_pr(repo, tmp_path):
    repo, _ = repo
    api = Api()
    runner.execute(job(repo, tmp_path, test="echo rotto && false"), api, run_agent=fake_agent(), gh=fake_gh([]))
    assert (api.updates[-1]["status"], api.updates[-1]["tests"]) == ("pr_open", "failed")


def test_secret_files_block_commit(repo, tmp_path):
    repo, _ = repo
    api = Api()

    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        (Path(cwd) / ".env").write_text("TOKEN=x\n")
        return 0, json.dumps({"result": "ok", "is_error": False})

    runner.execute(job(repo, tmp_path), api, run_agent=agent, gh=fake_gh([]))
    assert api.updates[-1]["status"] == "failed" and ".env" in api.updates[-1]["error"]


def test_command_rules_and_agy():
    assert runner.command_rules("./gradlew formatKotlin :app:testDebugUnitTest", "yarn lint && yarn test",
                                "(cd frontend && npm ci)") == [
        "Bash(./gradlew:*)", "Bash(cd:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git status:*)",
        "Bash(npm ci:*)", "Bash(yarn lint:*)", "Bash(yarn test:*)"]
    out = json.dumps({"status": "SUCCESS", "response": "fatto", "conversation_id": "c-1",
                      "denied_actions": [{"display_name": "RunCommand"}]})
    summary, error, limited = runner.parse_agent("agy", 1, out)
    assert error is None and "RunCommand" in summary
    assert runner.parse_agent("agy", 3, 'AGY_ERROR: {"code": "RESOURCE_EXHAUSTED"}')[2] is True
    assert runner.parse_agent("claude", 124, "")[1] == "agente oltre il tempo massimo"


def test_transient_agent_error_is_retried(repo, tmp_path, monkeypatch):
    repo, _ = repo
    monkeypatch.setattr(runner, "AGENT_RETRY_SECONDS", 0)
    api, calls = Api(), []
    good = fake_agent()

    def flaky(cmd, cwd, timeout, env, check=None, on_line=None):
        calls.append(1)
        if len(calls) == 1:
            return 3, 'AGY_ERROR: {"short_error": "request failed: read: operation timed out"}'
        return good(cmd, cwd, timeout, env)

    runner.execute(job(repo, tmp_path), api, run_agent=flaky, gh=fake_gh([]))
    assert len(calls) == 2 and api.updates[-1]["status"] == "pr_open"
    assert runner.transient('AGY_ERROR: {"retryable": true}')
    assert not runner.transient('AGY_ERROR: {"code": "RESOURCE_EXHAUSTED", "msg": "timeout"}')


def test_final_update_kept_until_delivered(tmp_path, monkeypatch):
    pending = tmp_path / "pending.jsonl"
    api = runner.Api("http://127.0.0.1:9", "t", pending=pending, retry_seconds=0)  # porta chiusa
    reconnects = []
    api.reconnect = lambda: reconnects.append(1)
    assert api.update(3, step="agent") is False and not pending.exists()  # non finale: non si salva
    assert api.update(3, status="failed", error="x") is False
    assert json.loads(pending.read_text()) == {"run_id": 3, "status": "failed", "error": "x"} and reconnects

    sent = []
    monkeypatch.setattr(api, "_post", lambda path, body: sent.append(body))
    api.flush_pending()
    assert sent == [{"run_id": 3, "status": "failed", "error": "x"}] and not pending.exists()


def test_agy_denied_command_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.providers.agy.AGY_CONVERSATIONS", tmp_path)
    (tmp_path / "c-1.db").write_bytes(b'xx{"CommandLine":"git status"}yy{"CommandLine":"adb logcat \\"Tripo\\""}zz')
    out = json.dumps({"response": "", "conversation_id": "c-1", "denied_actions": [{"display_name": "RunCommand"}]})
    summary, error, _ = runner.parse_agent("agy", 1, out)
    assert error is None and '`adb logcat "Tripo"`' in summary
    assert agy_last_command("../etc") is None


def test_sh_stops_long_command_when_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CHECK_SECONDS", 0.2)
    import time
    start = time.monotonic()
    code, out = runner.sh("sleep 30 & wait", tmp_path, 60, check=lambda: True)
    assert code == 130 and "fermato da Telegram" in out and time.monotonic() - start < 10
    assert runner.sh("echo ciao", tmp_path, 10, check=lambda: True)[1].strip() == "ciao"
    assert runner.sh("sleep 5", tmp_path, 1)[0] == 124


def test_cancel_during_agent(repo, tmp_path, monkeypatch):
    repo, _ = repo
    api = Api(cancel_after=1)  # il controllo dopo il setup passa, quello durante l'agente ferma

    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        return (130, "[fermato da Telegram]") if check and check() else (0, "{}")

    runner.execute(job(repo, tmp_path), api, run_agent=agent, gh=fake_gh([]))
    assert (api.updates[-1]["status"], api.updates[-1]["step"]) == ("cancelled", "agent")
    assert not (tmp_path / "wt" / "novainfo-7").exists()
    branches = subprocess.run(["git", "branch", "--list", "agent/*"], cwd=repo, capture_output=True, text=True).stdout
    assert branches.strip() == ""


def test_agent_env_blocks_push(repo, tmp_path):
    repo, _ = repo
    (repo / "x.txt").write_text("x")
    run("git", "add", "-A", cwd=repo)
    run("git", "commit", "-q", "-m", "x", cwd=repo)
    blocked = subprocess.run(["git", "push", "origin", "HEAD:refs/heads/prova"], cwd=repo, capture_output=True,
                             text=True, env=runner.agent_env(tmp_path / "hooks"))
    assert blocked.returncode != 0 and "push disabilitato" in blocked.stderr
    assert subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/prova"], cwd=repo).returncode == 0


def test_submodule_change_opens_two_prs(tmp_path):
    def init(path, remote):
        run("git", "init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
        run("git", "clone", "-q", str(remote), str(path), cwd=tmp_path)
        for k, v in (("user.email", "d@example.com"), ("user.name", "Alex")):
            run("git", "config", k, v, cwd=path)

    fw, fw_remote = tmp_path / "fw", tmp_path / "fw.git"
    init(fw, fw_remote)
    (fw / "lib.txt").write_text("v1\n")
    run("git", "add", "-A", cwd=fw)
    run("git", "commit", "-q", "-m", "v1", cwd=fw)
    run("git", "push", "-q", "origin", "main", cwd=fw)
    (fw / "local.properties").write_text("sdk.dir=/segreto\n")
    app, app_remote = tmp_path / "app", tmp_path / "app.git"
    init(app, app_remote)
    (app / "app.txt").write_text("v1\n")
    run("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(fw_remote), "framework", cwd=app)
    run("git", "add", "-A", cwd=app)
    run("git", "commit", "-q", "-m", "init", cwd=app)
    run("git", "push", "-q", "origin", "main", cwd=app)
    run("git", "remote", "set-head", "origin", "main", cwd=app)

    j = job(app, tmp_path, name="my-app", test=None, copy_files=[],
            setup="git -c protocol.file.allow=always submodule update --init")
    j.update(run_id=9, branch="agent/9", repo="x/app", focus="framework",
             task={**j["task"], "external_id": "x/fw#12", "title": "Process death"},
             submodules=[{"path": "framework", "project": "fw", "repo": "x/fw", "local_path": str(fw),
                          "test": "test -f lib.txt", "copy_files": ["local.properties"]}])

    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        assert "`framework` è un submodule" in cmd[2] and "Il task è del submodule `framework`" in cmd[2]
        (Path(cwd) / "framework" / "lib.txt").write_text("v2\n")
        (Path(cwd) / "app.txt").write_text("v2\n")
        return 0, json.dumps({"result": "Fix nel framework.", "is_error": False, "subtype": "success"})

    calls = []

    def gh(cmd, cwd, timeout, env=None):
        calls.append((cmd, Path(cwd).name))
        if cmd[2] == "edit":
            return 0, ""
        return 0, "https://github.com/x/fw/pull/5\n" if Path(cwd).name == "framework" else "https://github.com/x/app/pull/7\n"

    api = Api()
    runner.execute(j, api, run_agent=agent, gh=gh)
    final = api.updates[-1]
    assert (final["status"], final["pr_url"], final["sub_pr_urls"], final["tests"]) == (
        "pr_open", "https://github.com/x/app/pull/7", ["https://github.com/x/fw/pull/5"], "passed")
    (fw_pr, fw_dir), (app_pr, app_dir), (edit, _) = calls
    fw_body, app_body = fw_pr[fw_pr.index("--body") + 1], app_pr[app_pr.index("--body") + 1]
    assert fw_dir == "framework" and "Closes x/fw#12" in fw_body
    assert "https://github.com/x/fw/pull/5" in app_body and "Closes" not in app_body and "Ordine di merge" in app_body
    assert edit[:4] == ["gh", "pr", "edit", "https://github.com/x/fw/pull/5"]
    assert "https://github.com/x/app/pull/7" in edit[edit.index("--body") + 1]
    fw_head = subprocess.run(["git", "rev-parse", "agent/9"], cwd=fw_remote, capture_output=True,
                             text=True).stdout.strip()
    files = subprocess.run(["git", "show", "--name-only", "--format=%s", "agent/9"], cwd=fw_remote,
                           capture_output=True, text=True).stdout
    assert "lib.txt" in files and "local.properties" not in files
    tree = subprocess.run(["git", "ls-tree", "agent/9", "framework", "app.txt"], cwd=app_remote,
                          capture_output=True, text=True).stdout
    assert fw_head in tree and "app.txt" in tree
    assert not (tmp_path / "wt" / "my-app-9").exists()  # worktree con submodule rimosso
def test_agy_skip_permissions(repo, tmp_path):
    repo, _ = repo
    j = job(repo, tmp_path)
    j["executor"] = {"name": "antigravity", "cli": "agy", "model": "gemini-3.8-flash-high", "skip_permissions": True}
    cmd = runner.agent_command(j, "p")
    assert "--dangerously-skip-permissions" in cmd and "--mode" not in cmd
    assert "niente push" in runner.build_prompt(j) and "viene negato" not in runner.build_prompt(j)
    j["executor"]["skip_permissions"] = False
    assert runner.agent_command(j, "p")[5:7] == ["--mode", "accept-edits"]
    assert "`git grep`" in runner.build_prompt(j) and "viene negato" in runner.build_prompt(j)


AGY_REVIEWER = {"name": "antigravity", "cli": "agy", "model": "gemini-3.8-flash-high", "family": "google"}


def usage(gemini=0.5):
    return lambda: [{"cli": "agy", "bucket": "gemini-5h", "kind": "snapshot", "remaining_fraction": gemini,
                     "reset_at": "2026-10-16T01:00:00Z"}]


def test_review_finds_problems_and_executor_fixes_once(repo, tmp_path):
    repo, remote = repo
    j = job(repo, tmp_path)
    j.update(reviewer=AGY_REVIEWER, review={"fix_rounds": 1})
    prompts = []

    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        prompts.append((cmd[0], cmd[2]))
        if cmd[0] == "agy":  # revisore: sola lettura, sandbox, review strutturata
            assert "--sandbox" in cmd and "--json-schema" in cmd and "accept-edits" not in cmd
            assert "+v2" in cmd[2]  # il diff del lavoro è nel prompt
            review = {"verdict": "changes", "summary": "Manca un caso.",
                      "issues": [{"severity": "major", "file": "app.txt", "problem": "manca v3", "fix": "aggiungi"},
                                 {"severity": "minor", "problem": "stile"}]}
            return 0, json.dumps({"status": "SUCCESS", "response": "", "structured_output": review})
        fixing = "<review>" in cmd[2]
        (Path(cwd) / "app.txt").write_text("v3\n" if fixing else "v2\n")
        assert "Skip advisor checkpoint 3" in cmd[2]
        return 0, json.dumps({"result": "Aggiunto v3." if fixing else "Fatto v2.", "is_error": False})

    api, calls = Api(), []
    runner.execute(j, api, run_agent=agent, gh=fake_gh(calls), usage=usage())
    final = api.updates[-1]
    assert final["status"] == "pr_open" and [p[0] for p in prompts] == ["claude", "agy", "claude"]
    assert final["review"]["rounds"] == 1 and final["review"]["fix_summary"] == "Aggiunto v3."
    assert final["quota"][0]["bucket"] == "gemini-5h"
    assert [u.get("step") for u in api.updates[:-1]] == ["worktree", "agent", "tests", "review", "fix", "tests",
                                                          "push", "pr"]
    body = calls[0][calls[0].index("--body") + 1]
    assert "## Review incrociata (antigravity)" in body and "**major** `app.txt`: manca v3 → aggiungi" in body
    assert "### Correzioni dopo la review" in body and "Aggiunto v3." in body
    content = subprocess.run(["git", "show", "agent/7:app.txt"], cwd=remote, capture_output=True, text=True).stdout
    assert content == "v3\n"


def test_reviewer_edits_are_discarded_and_review_errors_do_not_block(repo, tmp_path):
    repo, remote = repo
    j = job(repo, tmp_path)
    j.update(reviewer={"name": "claude-sonnet", "cli": "claude", "model": "sonnet", "family": "anthropic"})

    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        if "--tools" in cmd:  # revisore Claude con i soli strumenti di lettura
            assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob"
            (Path(cwd) / "app.txt").write_text("rovinato\n")
            (Path(cwd) / "nuovo.txt").write_text("x")
            return 0, json.dumps({"is_error": True, "subtype": "error", "result": "usage limit reached"})
        (Path(cwd) / "app.txt").write_text("v2\n")
        return 0, json.dumps({"result": "ok", "is_error": False})

    api, calls = Api(), []
    runner.execute(j, api, run_agent=agent, gh=fake_gh(calls))
    final = api.updates[-1]
    assert final["status"] == "pr_open" and "usage limit" in final["review"]["error"]
    assert {"cli": "claude", "kind": "rate_limited", "reset_at": None} in final["quota"]
    body = calls[0][calls[0].index("--body") + 1]
    assert "Non eseguita:" in body and "il revisore ha modificato file" in body
    files = subprocess.run(["git", "show", "--name-only", "--format=", "agent/7"], cwd=remote,
                           capture_output=True, text=True).stdout.split()
    assert files == ["app.txt"]
    assert subprocess.run(["git", "show", "agent/7:app.txt"], cwd=remote, capture_output=True,
                          text=True).stdout == "v2\n"


def test_agy_exhausted_quota_fails_before_starting(repo, tmp_path):
    repo, _ = repo
    j = job(repo, tmp_path)
    j["executor"] = {"name": "antigravity", "cli": "agy", "model": "gemini-3.8-flash-high", "family": "google"}
    api = Api()
    runner.execute(j, api, run_agent=lambda *a, **k: pytest.fail("agy non deve partire"), gh=fake_gh([]),
                   usage=usage(gemini=0))
    final = api.updates[-1]
    assert (final["status"], final["rate_limited"]) == ("failed", True) and "gemini-5h" in final["error"]
    assert [q["kind"] for q in final["quota"]] == ["snapshot", "exhausted"]


def test_agy_usage_parsing():
    out = json.dumps({"command": {"data": {"groups": [
        {"buckets": [{"id": "gemini-weekly", "remaining_fraction": 0.9, "reset_time": "t1"},
                     {"id": "gemini-5h", "remaining_fraction": 0, "reset_time": "t2"}]},
        {"buckets": [{"id": "3p-5h", "remaining_fraction": 1, "reset_time": "t3"}]}]}}})
    agy = providers.get("agy")
    buckets = agy.usage(lambda *a: (0, out))
    assert [b["bucket"] for b in buckets] == ["gemini-weekly", "gemini-5h", "3p-5h"]
    assert [b["bucket"] for b in agy.exhausted("gemini-3.8-flash-high", buckets)] == ["gemini-5h"]
    assert agy.exhausted("claude-sonnet-4.5", buckets) == []
    assert agy.usage(lambda *a: (1, "boom")) == []


def test_realign_moves_submodule_to_merged_commit(tmp_path):
    def init(path, remote):
        run("git", "init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
        run("git", "clone", "-q", str(remote), str(path), cwd=tmp_path)
        for k, v in (("user.email", "d@example.com"), ("user.name", "Alex")):
            run("git", "config", k, v, cwd=path)

    fw, fw_remote = tmp_path / "fw", tmp_path / "fw.git"
    init(fw, fw_remote)
    (fw / "lib.txt").write_text("v1\n")
    run("git", "add", "-A", cwd=fw)
    run("git", "commit", "-q", "-m", "v1", cwd=fw)
    run("git", "push", "-q", "origin", "main", cwd=fw)
    app, app_remote = tmp_path / "app", tmp_path / "app.git"
    init(app, app_remote)
    run("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(fw_remote), "framework", cwd=app)
    run("git", "commit", "-q", "-m", "init", cwd=app)
    run("git", "push", "-q", "origin", "main", cwd=app)
    run("git", "checkout", "-q", "-b", "agent/1", cwd=app)  # PR app del run 1
    (app / "app.txt").write_text("v2\n")
    run("git", "add", "-A", cwd=app)
    run("git", "commit", "-q", "-m", "x\n\nRun Jarvis #1.", cwd=app)
    run("git", "push", "-q", "origin", "agent/1", cwd=app)
    run("git", "checkout", "-q", "main", cwd=app)
    (fw / "lib.txt").write_text("v2\n")  # lo squash merge sul framework
    run("git", "commit", "-qam", "squash (#5)", cwd=fw)
    run("git", "push", "-q", "origin", "main", cwd=fw)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=fw, capture_output=True, text=True).stdout.strip()

    j = job(app, tmp_path, name="my-app", test="test -f app.txt", copy_files=[],
            setup="git -c protocol.file.allow=always submodule update --init")
    j.update(kind="realign", run_id=2, realign={"branch": "agent/1", "pr_url": "https://github.com/x/app/pull/7",
                                                "parent_run_id": 1, "path": "framework", "sha": sha,
                                                "sub_pr": "https://github.com/x/fw/pull/5"})
    api = Api()
    runner.execute_realign(j, api)
    final = api.updates[-1]
    assert (final["status"], final["tests"], final["pr_url"]) == ("done", "passed", "https://github.com/x/app/pull/7")
    tree = subprocess.run(["git", "ls-tree", "agent/1", "framework"], cwd=app_remote, capture_output=True,
                          text=True).stdout
    log = subprocess.run(["git", "log", "-2", "--format=%s", "agent/1"], cwd=app_remote, capture_output=True,
                         text=True).stdout.splitlines()
    assert sha in tree and log == [f"Realign framework to merged {sha[:10]}", "x"]
    assert not (tmp_path / "wt" / "my-app-2").exists()


JUNIT = """<testsuite><testcase classname="co.acme.TaxiHomeViewModelTest" name="noTaxiFound">
<failure message="expected NoTaxiFound but was ScheduledBookingUnavailable">trace</failure></testcase>
<testcase classname="co.acme.TaxiHomeViewModelTest" name="ok"/></testsuite>"""


def test_failed_tests_trigger_fix_round_and_agy_retries_after_denied_command(repo, tmp_path, monkeypatch):
    repo, remote = repo
    monkeypatch.setattr(runner, "LOGS", tmp_path / "logs")
    j = job(repo, tmp_path, test="test \"$(cat app.txt)\" = v3 || "
            "(mkdir -p m/build/test-results/t && printf '%s' \"$JUNIT\" > m/build/test-results/t/TEST-x.xml; false)")
    monkeypatch.setenv("JUNIT", JUNIT)
    j.update(reviewer=AGY_REVIEWER, review={"fix_rounds": 1})
    seen = []

    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        seen.append(cmd[0])
        if cmd[0] == "agy":
            assert "co.acme.TaxiHomeViewModelTest.noTaxiFound: expected NoTaxiFound" in cmd[2]
            if seen.count("agy") == 1:  # primo tentativo: comando negato, niente review
                return 0, json.dumps({"status": "SUCCESS", "response": "", "denied_actions": [{"x": 1}]})
            assert "comando di shell negato" in cmd[2]
            return 0, json.dumps({"structured_output": {"verdict": "ok", "summary": "Va bene.", "issues": []}})
        if "<review>" in cmd[2]:
            assert '"test_falliti"' in cmd[2] and "noTaxiFound" in cmd[2]
            (Path(cwd) / "app.txt").write_text("v3\n")
            return 0, json.dumps({"result": "Aggiornato il test.", "is_error": False})
        (Path(cwd) / "app.txt").write_text("v2\n")
        return 0, json.dumps({"result": "Fatto.", "is_error": False})

    api, calls = Api(), []
    runner.execute(j, api, run_agent=agent, gh=fake_gh(calls), usage=usage())
    final = api.updates[-1]
    assert seen == ["claude", "agy", "agy", "claude"]
    assert (final["status"], final["tests"], final["review"]["rounds"]) == ("pr_open", "passed", 1)
    assert (tmp_path / "logs" / "run-7-review-1.txt").exists() and (tmp_path / "logs" / "run-7-fix-1.txt").exists()
    body = calls[0][calls[0].index("--body") + 1]
    assert "Aggiornato il test." in body and "✅ passati" in body


def test_failed_tests_without_reviewer_still_get_a_fix(repo, tmp_path, monkeypatch):
    repo, _ = repo
    monkeypatch.setattr(runner, "LOGS", tmp_path / "logs")
    j = job(repo, tmp_path, test='test "$(cat app.txt)" = v3')

    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        fixing = "<review>" in cmd[2]
        (Path(cwd) / "app.txt").write_text("v3\n" if fixing else "v2\n")
        return 0, json.dumps({"result": "Corretto." if fixing else "Fatto.", "is_error": False})

    api, calls = Api(), []
    runner.execute(j, api, run_agent=agent, gh=fake_gh(calls))
    assert api.updates[-1]["tests"] == "passed"
    assert "## Correzioni dopo i test falliti" in calls[0][calls[0].index("--body") + 1]


def test_repair_fixes_tests_on_the_existing_branches(tmp_path):
    def init(path, remote):
        run("git", "init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
        run("git", "clone", "-q", str(remote), str(path), cwd=tmp_path)
        for k, v in (("user.email", "d@example.com"), ("user.name", "Alex")):
            run("git", "config", k, v, cwd=path)

    fw, fw_remote = tmp_path / "fw", tmp_path / "fw.git"
    init(fw, fw_remote)
    (fw / "lib.txt").write_text("v1\n")
    run("git", "add", "-A", cwd=fw)
    run("git", "commit", "-q", "-m", "v1", cwd=fw)
    run("git", "push", "-q", "origin", "main", cwd=fw)
    run("git", "checkout", "-q", "-b", "agent/10", cwd=fw)  # PR del framework del run 10
    (fw / "lib.txt").write_text("v2\n")
    run("git", "commit", "-qam", "fix\n\nRun Jarvis #10.", cwd=fw)
    run("git", "push", "-q", "origin", "agent/10", cwd=fw)
    app, app_remote = tmp_path / "app", tmp_path / "app.git"
    init(app, app_remote)
    run("git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(fw_remote), "framework", cwd=app)
    run("git", "-C", "framework", "checkout", "-q", "main", cwd=app)
    run("git", "add", "-A", cwd=app)
    run("git", "commit", "-q", "-m", "init", cwd=app)
    run("git", "push", "-q", "origin", "main", cwd=app)
    run("git", "remote", "set-head", "origin", "main", cwd=app)
    run("git", "checkout", "-q", "-b", "agent/10", cwd=app)  # PR app del run 10, puntatore su agent/10
    run("git", "-C", "framework", "fetch", "-q", "origin", "agent/10", cwd=app)
    run("git", "-C", "framework", "checkout", "-q", "FETCH_HEAD", cwd=app)
    run("git", "add", "framework", cwd=app)
    run("git", "commit", "-q", "-m", "x\n\nRun Jarvis #10.", cwd=app)
    run("git", "push", "-q", "origin", "agent/10", cwd=app)
    run("git", "checkout", "-q", "main", cwd=app)

    fw_pr, app_pr = "https://github.com/x/fw/pull/5", "https://github.com/x/app/pull/7"
    j = job(app, tmp_path, name="my-app", test=None, copy_files=[],
            setup="git -c protocol.file.allow=always submodule update --init")
    j.update(kind="repair", run_id=11, branch="agent/11", repo="x/app", reviewer=AGY_REVIEWER,
             repair={"branch": "agent/10", "pr_url": app_pr, "parent_run_id": 10, "sub_pr_urls": [fw_pr]},
             submodules=[{"path": "framework", "project": "fw", "repo": "x/fw", "local_path": str(fw),
                          "test": 'test "$(cat lib.txt)" = v3', "copy_files": []}])
    seen = []

    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        seen.append(cmd[0])
        if cmd[0] == "agy":
            assert "-v1" in cmd[2] and "+v3" in cmd[2]  # review di tutto il lavoro, dal commit di base
            return 0, json.dumps({"structured_output": {"verdict": "ok", "summary": "Ok.", "issues": []}})
        assert '"test_falliti"' in cmd[2]
        (Path(cwd) / "framework" / "lib.txt").write_text("v3\n")
        return 0, json.dumps({"result": "Test sistemato.", "is_error": False})

    calls = []

    def gh(cmd, cwd, timeout, env=None):
        calls.append(cmd)
        return 0, ""

    api = Api()
    runner.execute(j, api, run_agent=agent, gh=gh, usage=usage())
    final = api.updates[-1]
    assert (final["status"], final["tests"], final["pr_url"], final["sub_pr_urls"]) == (
        "done", "passed", app_pr, [fw_pr]), final
    assert seen == ["claude", "agy"]  # niente lavoro da capo: prima la correzione dei test, poi la review
    assert [c[:4] for c in calls] == [["gh", "pr", "comment", fw_pr], ["gh", "pr", "comment", app_pr]]
    assert "Test sistemato." in calls[0][-1] and "✅ passati" in calls[0][-1]
    fw_head = subprocess.run(["git", "rev-parse", "agent/10"], cwd=fw_remote, capture_output=True,
                             text=True).stdout.strip()
    assert subprocess.run(["git", "show", "agent/10:lib.txt"], cwd=fw_remote, capture_output=True,
                          text=True).stdout == "v3\n"
    tree = subprocess.run(["git", "ls-tree", "agent/10", "framework"], cwd=app_remote, capture_output=True,
                          text=True).stdout
    assert fw_head in tree
    assert not (tmp_path / "wt" / "my-app-11").exists()

    # di nuovo: test ok e review ok, niente da pushare
    calls.clear()
    j["run_id"] = 12
    runner.execute(j, Api(), run_agent=agent, gh=gh, usage=usage())
    assert len(calls) == 2 and "Nessuna modifica necessaria" in calls[1][-1]
    assert subprocess.run(["git", "rev-parse", "agent/10"], cwd=fw_remote, capture_output=True,
                          text=True).stdout.strip() == fw_head


def test_review_has_its_own_timeout():
    cmd = runner.review_command(AGY_REVIEWER, "p", 15)
    assert cmd[cmd.index("--print-timeout") + 1] == "15m"
    assert runner.parse_review("agy", 124, "")[1] == "revisore oltre il tempo massimo"


def test_claude_quota_messages_are_recognised():
    assert runner.parse_agent("claude", 1, "Claude AI usage limit reached|1760000000\n")[2]
    short = json.dumps({"result": "You've hit your limit · resets 3pm (Europe/Rome)", "is_error": False})
    summary, error, limited = runner.parse_agent("claude", 0, short)
    assert limited and error.startswith("claude: You've hit your limit")
    long = json.dumps({"result": "Ho aggiunto il messaggio 'usage limit reached' alla UI. " * 10, "is_error": False})
    assert runner.parse_agent("claude", 0, long)[1:] == (None, False)

    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)  # 14:00 a Roma
    assert runner.claude_reset("limit reached|1760000000") == "2025-10-09T08:53:20+00:00"
    assert runner.claude_reset("resets 3pm (Europe/Rome)", now) == "2026-10-16T13:00:00+00:00"
    assert runner.claude_reset("resets 1:30pm (Europe/Rome)", now) == "2026-10-17T11:30:00+00:00"  # domani
    assert runner.claude_reset("resets Oct 20") is None
    assert runner.rate_limit_event("agy", "resets 3pm") == {"cli": "agy", "kind": "rate_limited", "reset_at": None}


def test_repair_passes_pr_comments_and_ci_to_the_executor(repo, tmp_path):
    repo, remote = repo
    run("git", "checkout", "-q", "-b", "agent/10", cwd=repo)
    (repo / "app.txt").write_text("v2\n")
    run("git", "commit", "-qam", "x\n\nRun Jarvis #10.", cwd=repo)
    run("git", "push", "-q", "origin", "agent/10", cwd=repo)
    run("git", "checkout", "-q", "main", cwd=repo)
    pr = "https://github.com/alex-dev/novainfo/pull/12"
    j = job(repo, tmp_path)
    j.update(kind="repair", run_id=11, branch="agent/11", repair={
        "branch": "agent/10", "pr_url": pr, "parent_run_id": 10, "sub_pr_urls": [],
        "feedback": {"comments": [{"id": 1, "pr": pr, "author": "mario", "at": "t", "file": "app.txt:1",
                                   "text": "usa v3"}],
                     "ci": [{"pr": pr, "name": "build", "conclusion": "failure", "summary": "lint"}]}})
    prompts = []

    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        prompts.append(cmd[2])
        (Path(cwd) / "app.txt").write_text("v3\n")
        return 0, json.dumps({"result": "Messo v3.", "is_error": False})

    calls, api = [], Api()
    runner.execute(j, api, run_agent=agent, gh=fake_gh(calls))
    assert api.updates[-1]["status"] == "done" and api.updates[-1]["tests"] == "passed"
    assert len(prompts) == 1  # test verdi, ma i commenti fanno partire la correzione
    assert '"commenti_pr"' in prompts[0] and '"testo": "usa v3"' in prompts[0] and '"check": "build"' in prompts[0]
    assert "Passati all'agente: 1 commenti di review, check falliti: `build`." in calls[0][-1]
    assert subprocess.run(["git", "show", "agent/10:app.txt"], cwd=remote, capture_output=True,
                          text=True).stdout == "v3\n"


# --- fase "piano" e log dal vivo ---

def test_sh_stream_calls_on_line_and_keeps_semantics(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CHECK_SECONDS", 0.2)
    seen = []
    code, out = runner.sh_stream("echo uno; echo due; echo errore >&2", tmp_path, 10, on_line=seen.append)
    assert code == 0 and seen == ["uno", "due"] and "uno\ndue\n" in out and "errore" in out
    import time
    start = time.monotonic()
    assert runner.sh_stream("sleep 30 & wait", tmp_path, 60, check=lambda: True)[0] == 130
    assert time.monotonic() - start < 10
    code, out = runner.sh_stream("sleep 5", tmp_path, 1)
    assert code == 124 and "timeout" in out
    assert runner.sh_stream("exit 3", tmp_path, 10)[0] == 3
    assert runner.sh_stream("echo x", tmp_path, 10, on_line=lambda line: 1 / 0)[1].strip() == "x"  # callback che rompe


def test_event_lines_show_actions_not_file_contents():
    root = "/Users/me/.jarvis/worktrees/my-app-7"
    claude = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "segreto"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": f"{root}/app/src/A.kt"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": f"{root}/B.kt", "old_string": "CONTENUTO"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "./gradlew test " + "x" * 300}},
            {"type": "tool_use", "name": "TodoWrite", "input": {"todos": []}},
            {"type": "text", "text": "Ho trovato la causa.\nSeconda riga"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "CONTENUTO DEL FILE"}]}},
    ]
    lines = [line for event in claude for line in claude_event_lines(event, root)]
    assert lines[0] == "📖 Read app/src/A.kt" and lines[1] == "✏️ Edit B.kt"
    assert lines[2].startswith("$ ./gradlew test") and len(lines[2]) <= 150
    assert lines[3] == "💬 Ho trovato la causa. Seconda riga" and len(lines) == 4
    assert not any("CONTENUTO" in line or "segreto" in line for line in lines)

    agy = [
        {"event": "init", "conversation_id": "c"},
        {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE", "tool_name": "view_file",
                                                 "tool_info": {"name": "view_file", "parameters": {
                                                     "AbsolutePath": f"{root}/C.kt"}}}},
        {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", "tool_name": "view_file"}},
        {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE", "tool_name": "run_command",
                                                 "tool_info": json.dumps({"name": "run_command", "parameters": {
                                                     "CommandLine": "git status"}})}},
        {"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE", "text_delta": "ok\n"}},
    ]
    assert [line for event in agy for line in agy_event_lines(event, root)] == [
        "📖 Read C.kt", "$ git status", "💬 ok"]


def test_log_feed_batches_masks_and_takes_the_rest():
    api = Api()
    feed = runner.LogFeed(api, 7, Path("/w"), flush_seconds=0)
    feed.line("$ curl -H 'Authorization: Bearer abc123def' x")  # gitleaks:allow (token finto: il test verifica che il log lo mascheri)
    feed.line("token=ghp_" + "a" * 30)
    assert api.logs[0] == "$ curl -H 'Authorization: *** x"  # il segreto è coperto, il resto della riga resta
    assert all("abc123" not in line and "ghp_" not in line for line in api.logs)
    slow = runner.LogFeed(api, 7, flush_seconds=999)
    slow.line("a")
    slow.line("b")
    assert slow.take() == ["a", "b"] and slow.take() == []
    on_line = slow.events("claude")
    on_line("non json")
    on_line(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ciao"}]}}))
    assert slow.take() == ["💬 ciao"]


def test_parse_agent_stream_json_results():
    claude = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "result", "subtype": "success", "result": "Fatto.", "is_error": False}),
        json.dumps({"type": "rate_limit_event"}),  # dopo il risultato: non deve prevalere
    ])
    assert runner.parse_agent("claude", 0, claude) == ("Fatto.", None, False)
    agy = "\n".join([
        json.dumps({"event": "init"}),
        json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "Piano ok", "conversation_id": "c"}}),
    ])
    assert runner.parse_agent("agy", 0, agy) == ("Piano ok", None, False)
    cmd = runner.agent_command(job(Path("/r"), Path("/t")), "p")
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd


PLAN_JOB = {"kind": "plan", "plan": {"timeout_minutes": 15}}


def plan_stream(text):
    return "\n".join([json.dumps({"type": "system"}),
                      json.dumps({"type": "result", "subtype": "success", "result": text, "is_error": False})])


def test_plan_run_is_read_only_and_returns_the_plan(repo, tmp_path):
    repo, remote = repo
    j = {**job(repo, tmp_path), **PLAN_JOB, "run_id": 21, "branch": "agent/21"}
    commands = []

    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        commands.append(cmd)
        assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob" and "--permission-mode" not in cmd
        assert "NON implementare" in cmd[2] and "crasha" in cmd[2] and "Fix crash" in cmd[2]
        on_line(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "app.txt"}}]}}))
        (Path(cwd) / "app.txt").write_text("rovinato\n")  # un agente che sgarra
        return 0, plan_stream("1. Causa\n2. Modifiche: app.txt")

    api = Api()
    runner.execute_plan(j, api, run_agent=agent)
    final = api.updates[-1]
    assert final["status"] == "done" and final["plan"].startswith("1. Causa") and "annullato" in final["plan"]
    assert final["log"] == ["📝 claude-sonnet prepara il piano (sola lettura)", "📖 Read app.txt"]
    assert "tests" not in final and "pr_url" not in final
    assert not (tmp_path / "wt" / "novainfo-21").exists()  # worktree rimosso
    branches = subprocess.run(["git", "branch", "--list", "agent/*"], cwd=repo, capture_output=True, text=True).stdout
    assert branches.strip() == ""
    assert subprocess.run(["git", "ls-remote", "--heads", "origin"], cwd=repo, capture_output=True,
                          text=True).stdout.count("agent/") == 0  # niente push
    assert (repo / "app.txt").read_text() == "v1\n"


def test_plan_revision_prompt_and_agy_flags(repo, tmp_path):
    repo, _ = repo
    j = {**job(repo, tmp_path), "kind": "plan", "run_id": 22, "branch": "agent/22",
         "executor": {"name": "antigravity", "cli": "agy", "model": "gemini-3.8-flash-high", "family": "google",
                      "skip_permissions": True},
         "plan": {"timeout_minutes": 7, "previous": "VECCHIO PIANO", "note": "aggiungi il changelog"}}

    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        assert cmd[0] == "agy" and cmd[cmd.index("--mode") + 1] == "plan" and "--sandbox" in cmd
        assert "--dangerously-skip-permissions" not in cmd and cmd[cmd.index("--print-timeout") + 1] == "7m"
        assert "VECCHIO PIANO" in cmd[2] and "aggiungi il changelog" in cmd[2]
        return 0, json.dumps({"event": "result", "result": {"response": "Piano nuovo", "status": "SUCCESS"}})

    api = Api()
    runner.execute_plan(j, api, run_agent=agent, usage=usage())
    assert api.updates[-1]["plan"] == "Piano nuovo"
    api2 = Api()
    runner.execute_plan(j, api2, run_agent=lambda *a, **k: pytest.fail("agy non deve partire"), usage=usage(gemini=0))
    assert api2.updates[-1]["status"] == "failed" and api2.updates[-1]["rate_limited"] is True


def test_plan_failures_and_stop(repo, tmp_path):
    repo, _ = repo
    j = {**job(repo, tmp_path), **PLAN_JOB, "run_id": 23, "branch": "agent/23"}
    api = Api()
    runner.execute_plan(j, api, run_agent=lambda *a, **k: (0, plan_stream("  ")))
    assert api.updates[-1]["status"] == "failed" and "nessun piano" in api.updates[-1]["error"]
    j["run_id"] = 24  # il fallimento lascia il worktree su disco: un altro run, un altro worktree
    api = Api(cancel_after=1)  # il controllo dopo il setup passa, quello durante l'agente ferma
    runner.execute_plan(j, api, run_agent=lambda cmd, cwd, t, env, check=None, on_line=None:
                        (130, "[fermato da Telegram]") if check and check() else (0, plan_stream("x")))
    assert api.updates[-1]["status"] == "cancelled"
    assert not (tmp_path / "wt" / "novainfo-24").exists()


def test_approved_plan_goes_into_the_agent_prompt_and_logs_reach_jarvis(repo, tmp_path):
    repo, _ = repo
    j = job(repo, tmp_path)
    j["owner"] = "Alex"
    j["approved_plan"] = "1. Tocca app.txt\n2. Niente altro"

    def agent(cmd, cwd, timeout, env, check=None, on_line=None):
        assert "Alex ha approvato questo piano" in cmd[2] and "1. Tocca app.txt" in cmd[2]
        on_line(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": str(Path(cwd) / "app.txt")}}]}}))
        (Path(cwd) / "app.txt").write_text("v2\n")
        return 0, json.dumps({"result": "Fatto.", "is_error": False})

    api, calls = Api(), []
    runner.execute(j, api, run_agent=agent, gh=fake_gh(calls))
    assert api.updates[-1]["status"] == "pr_open"
    sent = [*getattr(api, "logs", []), *api.updates[-1]["log"]]
    assert "🚀 claude-sonnet al lavoro" in sent and "✏️ Edit app.txt" in sent and "🧪 build e test" in sent
    assert "⬆️ commit e push" in sent and "📬 apro le PR" in sent


def test_done_status_is_a_final_update_and_survives_a_dead_tunnel(tmp_path, monkeypatch):
    """Il piano (e la riparazione) finiscono con status "done": se il tunnel è giù l'esito non va perso."""
    pending = tmp_path / "pending.jsonl"
    api = runner.Api("http://127.0.0.1:1", "t", pending=pending, retry_seconds=0)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    api.update(5, status="done", plan="1. Piano", log=["x"])
    assert json.loads(pending.read_text())["plan"] == "1. Piano"
    api.update(5, step="agent")  # un aggiornamento di avanzamento non va in coda
    assert len(pending.read_text().splitlines()) == 1


def test_owner_name_falls_back_when_the_server_does_not_send_it(repo, tmp_path):
    repo, _ = repo
    j = job(repo, tmp_path)
    j["approved_plan"] = "1. Tocca app.txt"
    prompt = runner.build_prompt(j)
    assert "Owner ha approvato questo piano" in prompt and "{owner}" not in prompt
    j["owner"] = "Ada"
    assert "Ada ha approvato questo piano" in runner.build_prompt(j)


def test_agy_may_read_the_submodules_of_the_project_only():
    agy = providers.get("agy")
    assert "git -C lib grep" in agy.allowed_commands([], ["lib"]) and "git -C lib ls-files" in agy.allowed_commands([], ["lib"])
    assert not [c for c in agy.allowed_commands([], []) if c.startswith("git -C")]


# --- connettività (un host / host separati) ---

def _env(tmp_path, monkeypatch, text="JARVIS_RUNNER_TOKEN=t\n"):
    for k in [k for k in runner.os.environ if k.startswith("JARVIS_")]:
        monkeypatch.delenv(k)
    f = tmp_path / "runner.env"
    f.write_text(text)
    return runner.load_env(f)


def test_load_env_one_host_has_no_tunnel(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    assert env["JARVIS_SSH_HOST"] == ""
    assert env["JARVIS_URL"] == "http://127.0.0.1:8090"


def test_load_env_server_url(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch, "JARVIS_RUNNER_TOKEN=t\nJARVIS_URL=http://jarvis:9000\n")
    assert env["JARVIS_URL"] == "http://jarvis:9000" and env["JARVIS_SSH_HOST"] == ""


def test_load_env_existing_mac_setup(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch, "JARVIS_RUNNER_TOKEN=t\nJARVIS_SSH_HOST=myserver\n")
    assert (env["JARVIS_SSH_HOST"], env["JARVIS_URL"]) == ("myserver", "http://127.0.0.1:8090")
    assert (env["JARVIS_SSH_LOCAL_PORT"], env["JARVIS_SSH_REMOTE_PORT"]) == (8090, 8090)


def test_load_env_split_ports(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch,
               "JARVIS_RUNNER_TOKEN=t\nJARVIS_SSH_HOST=box\nJARVIS_SSH_LOCAL_PORT=9100\nJARVIS_SSH_REMOTE_PORT=8000\n")
    assert env["JARVIS_URL"] == "http://127.0.0.1:9100"
    assert (env["JARVIS_SSH_LOCAL_PORT"], env["JARVIS_SSH_REMOTE_PORT"]) == (9100, 8000)


def test_load_env_bad_port(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        _env(tmp_path, monkeypatch, "JARVIS_RUNNER_TOKEN=t\nJARVIS_SSH_LOCAL_PORT=abc\n")


class _Api:
    def __init__(self, ok):
        self.ok = ok

    def healthy(self):
        return self.ok


def test_tunnel_without_host_never_spawns_ssh(monkeypatch):
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: pytest.fail("ssh spawned"))
    assert runner.Tunnel("", _Api(True)).ensure() is True
    assert runner.Tunnel("", _Api(False)).ensure() is False


def test_tunnel_ssh_command_uses_ports(monkeypatch):
    seen = {}

    class P:
        def poll(self):
            return None

    api = _Api(False)

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        api.ok = True
        return P()

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    assert runner.Tunnel("myserver", api, 9100, 8000).ensure() is True
    assert seen["cmd"][-3:] == ["-L", "9100:127.0.0.1:8000", "myserver"]


def test_login_shell_is_zsh_on_mac_and_passwd_shell_elsewhere(monkeypatch):
    runner.login_shell.cache_clear()
    try:
        monkeypatch.setattr(runner.sys, "platform", "darwin")
        assert runner.login_shell() == "/bin/zsh"
        runner.login_shell.cache_clear()
        monkeypatch.setattr(runner.sys, "platform", "linux")
        monkeypatch.setattr(runner.os, "access", lambda p, m: p == "/bin/bash")
        monkeypatch.setattr(runner.pwd, "getpwuid", lambda uid: type("P", (), {"pw_shell": "/bin/bash"})())
        assert runner.login_shell() == "/bin/bash"
        runner.login_shell.cache_clear()
        monkeypatch.setattr(runner.pwd, "getpwuid", lambda uid: type("P", (), {"pw_shell": "/nope"})())
        assert runner.login_shell() == "/bin/bash"
    finally:
        monkeypatch.undo()
        runner.login_shell.cache_clear()


def test_cli_not_installed_fails_before_starting_and_asks_for_the_fallback(repo, tmp_path, monkeypatch):
    repo, _ = repo
    monkeypatch.setattr("shutil.which", lambda name: None)
    j = job(repo, tmp_path)  # esecutore claude
    api = Api()
    runner.execute(j, api, run_agent=lambda *a, **k: pytest.fail("l'agente non deve partire"), gh=fake_gh([]))
    final = api.updates[-1]
    assert (final["status"], final["rate_limited"]) == ("failed", True) and "claude non pronto" in final["error"]
    assert {"cli": "claude", "kind": "rate_limited", "reset_at": None} in final["quota"]
