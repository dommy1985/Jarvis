"""Provider Claude Code (`claude -p`)."""

import json
import re
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.i18n import t
from jarvis.providers.base import QUOTA, Provider, ProviderError, last_result, tail
from jarvis.providers.events import claude_event_lines

# Risposta di claude -p a quota finita: a volte arriva come risultato normale (is_error false), quindi solo su testi brevi.
CLAUDE_LIMIT = re.compile(r"usage limit reached|limit reached\s*[∙·|]|hit your (?:usage |session |weekly )?limit",
                          re.IGNORECASE)
RESET_EPOCH = re.compile(r"\|(\d{10})\b")  # "Claude AI usage limit reached|1760000000"
RESET_TIME = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([\w/+-]+)\))?", re.IGNORECASE)


def command_rules(*commands: str | None) -> list[str]:
    """Regole Bash di Claude per i comandi di setup/build/test del progetto (es. Bash(./gradlew:*))."""
    rules = {"Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(cd:*)"}
    for command in filter(None, commands):
        for part in re.split(r"&&|\|\||;|\|", command):
            words = part.strip().strip("()").split()
            if not words or words[0] == "cd":
                continue
            two = words[0] in ("npx", "npm", "yarn", "flutter", "uv", "dart") and len(words) > 1
            rules.add(f"Bash({' '.join(words[:2]) if two else words[0]}:*)")
    return sorted(rules)


def claude_reset(text: str, now: datetime | None = None) -> str | None:
    """Orario di reset (ISO UTC) dal messaggio di quota di Claude: "...|1760000000" o "resets 3pm (Zone/City)"."""
    if m := RESET_EPOCH.search(text):
        return datetime.fromtimestamp(int(m.group(1)), timezone.utc).isoformat(timespec="seconds")
    m = RESET_TIME.search(text)
    if m is None:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm:
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    try:
        tz = ZoneInfo(m.group(4)) if m.group(4) else None
    except (ZoneInfoNotFoundError, ValueError):
        tz = None
    now = (now or datetime.now(timezone.utc)).astimezone(tz)  # senza fuso: quello della macchina
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


class ClaudeProvider(Provider):
    name = "claude"
    binary = "claude"
    capabilities = frozenset({"execute", "plan_structured", "review"})  # niente "quota": Claude non espone le quote
    uses_task_suffix = True

    def build_exec_cmd(self, executor: dict, prompt: str, *, commands: Sequence[str | None],
                       skip_permissions: bool, timeout_minutes: int) -> list[str]:
        return [
            "claude", "-p", prompt, "--model", executor["model"], "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits",
            "--allowedTools", ",".join(["Agent", "Task", *command_rules(*commands)]),
            "--disallowedTools", "Bash(git push:*),Bash(gh:*)",
            "--setting-sources", "project,local",
        ]

    def build_plan_cmd(self, executor: dict, prompt: str, *, timeout_minutes: int) -> list[str]:
        return ["claude", "-p", prompt, "--model", executor["model"], "--output-format", "stream-json", "--verbose",
                "--tools", "Read,Grep,Glob", "--setting-sources", "project,local", "--no-session-persistence"]

    def build_review_cmd(self, reviewer: dict, prompt: str, *, schema: dict, timeout_minutes: int) -> list[str]:
        return ["claude", "-p", prompt, "--model", reviewer["model"], "--output-format", "json",
                "--json-schema", json.dumps(schema), "--tools", "Read,Grep,Glob", "--setting-sources", "project,local",
                "--no-session-persistence"]

    def build_structured_cmd(self, model: str, system: str, message: str, schema: dict) -> list[str]:
        return [
            "claude", "-p", message,
            "--model", model,
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--system-prompt", system,
            "--tools", "",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]

    def parse_structured(self, returncode: int, stdout: str, stderr: str) -> dict:
        try:
            out = json.loads(stdout)
        except json.JSONDecodeError:
            raise ProviderError(f"claude -p exit {returncode}: {(stderr or stdout)[-500:]}") from None
        if out.get("is_error") or "structured_output" not in out:
            raise ProviderError(f"claude -p {out.get('subtype')}: {str(out.get('result'))[-500:]}")
        return out["structured_output"]

    def parse_output(self, code: int, output: str) -> tuple[str, str | None, bool]:
        if code == 124:
            return "", t("prov.agent_timeout"), False
        data = last_result(output)
        if data is None:  # es. limite raggiunto prima ancora di partire: messaggio in chiaro, niente JSON
            return "", t("prov.agent_unreadable", code=code, tail=tail(output, 5)), bool(QUOTA.search(output))
        summary = str(data.get("result") or "")
        if data.get("is_error"):
            return summary, f"claude: {data.get('subtype')}: {summary[-300:]}", bool(QUOTA.search(summary))
        if len(summary) < 400 and CLAUDE_LIMIT.search(summary):
            return summary, f"claude: {summary.strip()[:300]}", True
        return summary, None, False

    def parse_review(self, code: int, output: str) -> tuple[dict | None, str | None, bool]:
        if code == 124:
            return None, t("prov.reviewer_timeout"), False
        data = None
        for line in reversed(output.strip().splitlines()):
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if data is None:
            return None, t("prov.reviewer_unreadable", code=code, tail=tail(output, 3)), bool(QUOTA.search(output))
        if data.get("is_error"):
            text = str(data.get("result") or "")
            return None, f"claude: {data.get('subtype')}: {text[-300:]}", bool(QUOTA.search(text))
        review = data.get("structured_output")
        if not isinstance(review, dict) or not isinstance(review.get("issues"), list):
            return None, t("prov.no_review"), False
        return review, None, False

    def event_lines(self, event: dict, root: str | None = None) -> list[str]:
        return claude_event_lines(event, root)

    def allowed_commands(self, commands: Sequence[str | None], submodule_paths: Sequence[str]) -> list[str]:
        return [r[5:-3] for r in command_rules(*commands)]  # "Bash(x:*)" -> "x"

    def quota_reset(self, output: str) -> str | None:
        return claude_reset(output)
