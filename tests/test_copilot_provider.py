import json
from pathlib import Path

import pytest

from jarvis import providers
from jarvis.providers.base import ProviderError
from jarvis.providers.copilot import CopilotProvider, extract_json

FIX = Path(__file__).parent / "fixtures" / "providers" / "copilot"
SCHEMA = {"type": "object"}
cp = CopilotProvider()
JSONL = (FIX / "jsonl_output.jsonl").read_text()
NOT_LOGGED_IN = (FIX / "not_logged_in.txt").read_text()


def msg(content, phase="final_answer", **extra):
    return json.dumps({"type": "assistant.message", "data": {"content": content, "phase": phase, **extra}})


def test_registry_and_capabilities():
    assert isinstance(providers.get("copilot"), CopilotProvider)
    assert providers.can("copilot", "execute") and providers.can("copilot", "review")
    assert not providers.can("copilot", "plan_structured") and not providers.can("copilot", "quota")
    assert cp.check_ready() is None or "copilot" in cp.check_ready()


def test_exec_command_blocks_push_and_gh():
    skip = cp.build_exec_cmd({"model": "auto"}, "p", commands=[], skip_permissions=True, timeout_minutes=5)
    assert skip[:5] == ["copilot", "-p", "p", "--model", "auto"] and "--allow-all-tools" in skip
    assert {"--deny-tool=shell(git push)", "--deny-tool=shell(gh)"} <= set(skip) and "--allow-all" not in skip
    safe = cp.build_exec_cmd({"model": "m"}, "p", commands=["uv run pytest"], skip_permissions=False, timeout_minutes=5)
    assert "--allow-all-tools" not in safe and "--allow-tool=write" in safe and "--allow-tool=shell(uv run:*)" in safe
    assert safe[safe.index("--output-format") + 1] == "json"


def test_plan_review_structured_are_read_only():
    plan = cp.build_plan_cmd({"model": "m"}, "p", timeout_minutes=5)
    review = cp.build_review_cmd({"model": "m"}, "check", schema=SCHEMA, timeout_minutes=5)
    structured = cp.build_structured_cmd("m", "sys", "msg", SCHEMA)
    for cmd in (plan, review, structured):
        assert "--deny-tool=write" in cmd and "--deny-tool=shell" in cmd and "--yolo" not in cmd
    assert plan[plan.index("--mode") + 1] == "plan"
    assert json.dumps(SCHEMA) in review[2] and review[2].startswith("check")
    assert structured[2] == "sys\n\nmsg"  # lo schema non viene aggiunto: lo fa il livello generico
    assert "--disable-builtin-mcps" in structured


def test_parse_output_from_jsonl_fixture():
    assert cp.parse_output(0, JSONL) == ("PONG", None, False)
    assert cp.parse_output(0, "PONG\n") == ("PONG", None, False)  # testo con -s
    assert cp.parse_output(124, "")[1] == "agente oltre il tempo massimo"


def test_parse_output_errors():
    summary, error, limited = cp.parse_output(1, NOT_LOGGED_IN)
    assert "non autenticato" in error and not limited
    _, error, limited = cp.parse_output(1, "Error: Session limits: reached for this period")
    assert error.startswith("copilot:") and limited is True
    _, error, limited = cp.parse_output(1, 'Error: Model "x" from --model flag is not available.')
    assert "not available" in error and not limited
    failed = json.dumps({"type": "result", "exitCode": 1})
    assert cp.parse_output(0, failed)[1] is not None
    # un quota citato nella risposta riuscita non è un errore
    assert cp.parse_output(0, msg("rate limit docs"))[1:] == (None, False)


def test_parse_review():
    review = {"issues": [], "summary": "ok"}
    fenced = msg("Ecco:\n```json\n" + json.dumps(review) + "\n```")
    assert cp.parse_review(0, fenced) == (review, None, False)
    assert "non ha restituito" in cp.parse_review(0, msg("nessun json"))[1]
    assert "non ha restituito" in cp.parse_review(0, msg('{"a": 1}'))[1]
    assert "non autenticato" in cp.parse_review(1, NOT_LOGGED_IN)[1]
    assert cp.parse_review(124, "")[1] == "revisore oltre il tempo massimo"
    assert cp.parse_review(0, "")[1].startswith("output del revisore non leggibile")


def test_parse_structured():
    assert cp.parse_structured(0, msg('{"count":42}'), "") == {"count": 42}
    assert cp.parse_structured(0, (FIX / "structured_output.txt").read_text(), "") == {"count": 42}
    assert cp.parse_structured(0, msg('Sure!\n```json\n{"a": {"b": "}"}}\n```\nDone'), "") == {"a": {"b": "}"}}
    with pytest.raises(ProviderError, match="non autenticato"):
        cp.parse_structured(1, "", NOT_LOGGED_IN)
    with pytest.raises(ProviderError, match="nessun oggetto JSON"):
        cp.parse_structured(0, JSONL, "")
    with pytest.raises(ProviderError, match="rate limit"):
        cp.parse_structured(1, "", "Error: rate limit exceeded")


def test_extract_json_prefers_first_object():
    assert extract_json('x {"a": 1} y {"b": 2}') == {"a": 1}
    assert extract_json("[1] no object") is None


def test_final_answer_preferred_over_intermediate_messages():
    out = msg("Cerco i file", phase="commentary") + "\n" + msg("Fatto", phase="final_answer")
    assert cp.parse_output(0, out)[0] == "Fatto"


def test_event_lines_and_allowed_commands():
    event = {"type": "assistant.message", "data": {"content": "Leggo", "toolRequests": [
        {"name": "bash", "arguments": {"command": "git status"}}]}}
    assert cp.event_lines(event) == ["💬 Leggo", "$ git status"]
    assert cp.event_lines({"type": "assistant.message_delta", "data": {"deltaContent": "P"}}) == []
    assert cp.event_lines(json.loads(JSONL.splitlines()[13])) == ["💬 PONG"]
    allowed = cp.allowed_commands(["./gradlew test"], ["lib"])
    assert "./gradlew" in allowed and "git -C lib status" in allowed
