"""Provider Antigravity (`agy -p`)."""

import json
import logging
import re
from collections.abc import Callable, Sequence
from pathlib import Path

from jarvis.i18n import t
from jarvis.providers.base import QUOTA, Provider, ProviderError, last_result, tail
from jarvis.providers.events import agy_event_lines

log = logging.getLogger("jarvis.providers.agy")

AGY_CONVERSATIONS = Path("~/.gemini/antigravity-cli/conversations").expanduser()
# Comandi aggiunti (13/10/2026) a permissions.allow di ~/.gemini/antigravity-cli/settings.json:
# agy in headless nega tutto il resto e chiude il turno al primo comando negato.
AGY_COMMANDS = ["git status", "git diff", "git log", "git show", "git branch", "git ls-files", "git grep", "ls",
                "cat", "head", "tail", "grep", "find", "rg", "wc", "pwd", "./gradlew", "npm run", "npm test",
                "npx tsc", "npx jest", "yarn", "flutter", "dart"]
SUBMODULE_GIT = ("grep", "log", "show", "status", "diff", "ls-files")  # sola lettura, nei submodule del progetto


def agy_last_command(conversation_id: str | None) -> str | None:
    """Ultimo comando proposto da agy nella conversazione (quello negato), letto dal suo DB locale."""
    if not conversation_id or not re.fullmatch(r"[\w-]+", conversation_id):
        return None
    try:
        data = (AGY_CONVERSATIONS / f"{conversation_id}.db").read_bytes()
    except OSError:
        return None
    found = re.findall(rb'"CommandLine":"((?:[^"\\]|\\.){1,300})"', data)
    return found[-1].decode(errors="replace").encode().decode("unicode_escape", errors="replace") if found else None


def _error_line(output: str) -> str | None:
    """Errori di modello o quota: exit 3 e "AGY_ERROR: {...}"."""
    return next((line[10:].strip() for line in output.splitlines() if line.startswith("AGY_ERROR:")), None)


WAITING_LINE = re.compile(
    r"^(?:"
    r"Waiting(?:\s+for\b.*|\b(?=[.…]))"
    r"|Sto attendendo\b.*"
    r"|Sto eseguendo (?:la build|il comando|i test|`|\./|\bgit\b).*"
    r"|I have launched\b.*and will wait\b.*"
    r"|I am waiting\b.*"
    r"|I'll wait\b.*"
    r")[.…]?$",
    re.I,
)


def strip_waiting_lines(text: str) -> str:
    """Rimuove dall'output di agy le righe di notifica di attesa o lancio comandi fuori dai blocchi di codice."""
    lines: list[str] = []
    in_code_block = False
    for line in text.splitlines():
        trimmed = line.strip()
        if trimmed.startswith("```"):
            in_code_block = not in_code_block
            lines.append(line)
            continue
        if in_code_block or len(trimmed) > 300 or not WAITING_LINE.match(trimmed):
            lines.append(line)
    cleaned = "\n".join(lines).strip()
    return cleaned or text.strip()


class AgyProvider(Provider):
    name = "agy"
    binary = "agy"
    # plan_structured: --json-schema nativo, verificato con la CLI vera il 20/10/2026 (agy 1.4.3, structured_output).
    capabilities = frozenset({"execute", "plan_structured", "review", "quota"})

    def build_exec_cmd(self, executor: dict, prompt: str, *, commands: Sequence[str | None],
                       skip_permissions: bool, timeout_minutes: int) -> list[str]:
        # agy non ha allowlist per sessione: in headless esegue solo i comandi già permessi nelle sue
        # impostazioni globali e si ferma al primo negato. Con skip_permissions (scelta dell'owner,
        # 13/10/2026) li esegue tutti; push, gh e credenziali git restano spenti da agent_env() del runner.
        permissions = ["--dangerously-skip-permissions"] if skip_permissions else ["--mode", "accept-edits"]
        return ["agy", "-p", prompt, "--model", executor["model"], *permissions,
                "--output-format", "stream-json", "--print-timeout", f"{timeout_minutes}m"]

    def build_plan_cmd(self, executor: dict, prompt: str, *, timeout_minutes: int) -> list[str]:
        # plan mode + sandbox anche con skip_permissions: il piano non deve poter cambiare niente
        return ["agy", "-p", prompt, "--model", executor["model"], "--mode", "plan", "--sandbox",
                "--output-format", "stream-json", "--print-timeout", f"{timeout_minutes}m"]

    def build_review_cmd(self, reviewer: dict, prompt: str, *, schema: dict, timeout_minutes: int) -> list[str]:
        return ["agy", "-p", prompt, "--model", reviewer["model"], "--sandbox", "--output-format", "json",
                "--json-schema", json.dumps(schema), "--print-timeout", f"{timeout_minutes}m"]

    def build_structured_cmd(self, model: str, system: str, message: str, schema: dict) -> list[str]:
        # Lo schema lo applica la CLI (--json-schema). agy non ha un flag per il
        # system prompt né per spegnere gli strumenti: il system va nel prompt, plan mode + sandbox li rendono inoffensivi.
        return ["agy", "-p", f"{system}\n\n{message}", "--model", model, "--mode", "plan", "--sandbox",
                "--output-format", "json", "--json-schema", json.dumps(schema)]

    def parse_structured(self, returncode: int, stdout: str, stderr: str) -> dict:
        if error := _error_line(stdout + "\n" + stderr):
            raise ProviderError(f"agy -p: {error[:500]}")
        data = last_result(stdout)
        if data is None:
            raise ProviderError(f"agy -p exit {returncode}: {(stderr or stdout)[-500:]}")
        output = data.get("structured_output")
        if not isinstance(output, dict):
            try:
                output = json.loads(str(data.get("response") or ""))
            except json.JSONDecodeError:
                output = None
        if not isinstance(output, dict):
            raise ProviderError(f"agy -p {data.get('status')}: {str(data.get('response'))[-500:]}")
        return output

    def parse_output(self, code: int, output: str) -> tuple[str, str | None, bool]:
        if code == 124:
            return "", t("prov.agent_timeout"), False
        data = last_result(output)
        if error := _error_line(output):
            return "", f"agy: {error[:600]}", bool(QUOTA.search(error))
        summary = strip_waiting_lines(str((data or {}).get("response") or ""))
        denied = (data or {}).get("denied_actions")
        if denied:  # non bloccante: le modifiche fatte restano, build e test li fa il runner
            command = agy_last_command((data or {}).get("conversation_id"))
            names = ", ".join(d.get("display_name", "?") for d in denied)
            summary += t("prov.agy_denied", names=names, command=f" `{command}`" if command else "")
        return summary, None, False

    def parse_review(self, code: int, output: str) -> tuple[dict | None, str | None, bool]:
        if code == 124:
            return None, t("prov.reviewer_timeout"), False
        if error := _error_line(output):
            return None, f"agy: {error[:300]}", bool(QUOTA.search(error))
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
            return None, f"agy: {data.get('subtype')}: {text[-300:]}", bool(QUOTA.search(text))
        review = data.get("structured_output")
        if not isinstance(review, dict) or not isinstance(review.get("issues"), list):
            return None, t("prov.no_review"), False
        return review, None, False

    def event_lines(self, event: dict, root: str | None = None) -> list[str]:
        return agy_event_lines(event, root)

    def allowed_commands(self, commands: Sequence[str | None], submodule_paths: Sequence[str]) -> list[str]:
        return AGY_COMMANDS + [f"git -C {path} {c}" for path in submodule_paths for c in SUBMODULE_GIT]

    def usage(self, run: Callable) -> list[dict]:
        """Quote (`/usage` non consuma quota) come eventi 'snapshot' per jarvis. [] se non leggibili."""
        code, out = run(["agy", "-p", "/usage", "--output-format", "json", "--print-timeout", "60s"], Path.home(), 90)
        try:
            data = json.loads(next(line for line in reversed(out.strip().splitlines()) if line.startswith("{")))
            groups = data["command"]["data"]["groups"]
        except (StopIteration, json.JSONDecodeError, KeyError, TypeError):
            log.warning("agy /usage non leggibile (exit %s): %s", code, tail(out, 3))
            return []
        return [{"cli": "agy", "bucket": b["id"], "kind": "snapshot", "remaining_fraction": b.get("remaining_fraction"),
                 "reset_at": b.get("reset_time")} for g in groups for b in g.get("buckets", [])]

    def exhausted(self, model: str, buckets: list[dict]) -> list[dict]:
        """Bucket a zero per il gruppo del modello (gemini-* per Gemini, 3p-* per Claude/GPT via agy)."""
        prefix = "gemini-" if model.startswith("gemini") else "3p-"
        return [b for b in buckets if b["bucket"].startswith(prefix) and (b["remaining_fraction"] or 0) <= 0]
