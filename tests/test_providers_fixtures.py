import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "providers"


def test_agy_fixtures():
    agy_dir = FIXTURES_DIR / "agy"
    assert agy_dir.is_dir()

    # Text output
    text = (agy_dir / "text_output.txt").read_text().strip()
    assert text == "PONG"

    # JSON output
    data = json.loads((agy_dir / "json_output.json").read_text())
    assert data["status"] == "SUCCESS"
    assert "response" in data
    assert "usage" in data

    # Structured output
    structured = json.loads((agy_dir / "structured_output.json").read_text())
    assert structured["status"] == "SUCCESS"
    assert structured["structured_output"] == {"count": 42}

    # Usage output
    usage = json.loads((agy_dir / "usage_output.json").read_text())
    assert usage["command"]["name"] == "usage"
    groups = usage["command"]["data"]["groups"]
    assert len(groups) >= 2
    bucket_ids = [b["id"] for g in groups for b in g["buckets"]]
    assert "gemini-weekly" in bucket_ids
    assert "3p-weekly" in bucket_ids


def test_claude_fixtures():
    claude_dir = FIXTURES_DIR / "claude"
    assert claude_dir.is_dir()

    # Text output
    text = (claude_dir / "text_output.txt").read_text().strip()
    assert text == "PONG"

    # JSON output
    data = json.loads((claude_dir / "json_output.json").read_text())
    assert data["type"] == "result"
    assert data["result"] == "PONG"
    assert not data["is_error"]

    # Structured output
    structured = json.loads((claude_dir / "structured_output.json").read_text())
    assert structured["structured_output"] == {"count": 42}

    # Usage output
    usage = json.loads((claude_dir / "usage_output.json").read_text())
    assert usage["local_command"] == "usage"
    assert "subscription" in usage["result"]


def test_copilot_fixtures():
    copilot_dir = FIXTURES_DIR / "copilot"
    assert copilot_dir.is_dir()

    # Text output
    text = (copilot_dir / "text_output.txt").read_text().strip()
    assert text == "PONG"

    # Silent text output
    silent_text = (copilot_dir / "text_silent_output.txt").read_text().strip()
    assert silent_text == "PONG"

    # Stderr stats
    stderr_stats = (copilot_dir / "stderr_stats.txt").read_text()
    assert "AI Credits" in stderr_stats

    # JSONL output
    lines = [json.loads(line) for line in (copilot_dir / "jsonl_output.jsonl").read_text().splitlines() if line.strip()]
    event_types = [evt.get("type") for evt in lines]
    assert "assistant.message" in event_types
    assert "result" in event_types

    # Find assistant message content
    assistant_msg = next(evt for evt in lines if evt.get("type") == "assistant.message")
    assert assistant_msg["data"]["content"] == "PONG"

    # Structured output
    structured_raw = (copilot_dir / "structured_output.txt").read_text().strip()
    parsed_structured = json.loads(structured_raw)
    assert parsed_structured == {"count": 42}

    # Not logged in error output
    not_logged_in = (copilot_dir / "not_logged_in.txt").read_text()
    assert "No authentication information found" in not_logged_in
