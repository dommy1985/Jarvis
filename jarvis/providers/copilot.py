"""Provider GitHub Copilot CLI (`copilot -p`). Fatti verificati: docs/oss-providers.md §2.3 e tests/fixtures/providers/copilot."""

import json
import re
from collections.abc import Sequence

from jarvis.i18n import t
from jarvis.providers.base import QUOTA, Provider, ProviderError, tail
from jarvis.providers.claude import command_rules
from jarvis.providers.events import short, tool_line

SUBMODULE_GIT = ("grep", "log", "show", "status", "diff", "ls-files")  # sola lettura, nei submodule del progetto
# Verificato (fixture not_logged_in.txt): exit 1 + "Error: No authentication information found."
NOT_LOGGED_IN = re.compile(r"No authentication information found", re.IGNORECASE)
MODEL_UNAVAILABLE = re.compile(r'Model ".*" from --model flag is not available')
# Euristica, non verificata da fixture: limiti di sessione/crediti di Copilot.
LIMIT = re.compile(r"Session limits?:|credits? (?:used|exhausted)|max.?ai.?credits", re.IGNORECASE)
# push e gh restano spenti (deny batte sempre allow, anche --allow-all-tools: `copilot help permissions`).
DENY = ["--deny-tool=shell(git push)", "--deny-tool=shell(gh)"]
READ_ONLY = ["--mode", "plan", "--allow-all-tools", "--deny-tool=write", "--deny-tool=shell", "--disable-builtin-mcps"]
COMMON = ["--output-format", "json", "--no-ask-user"]


def parse_events(output: str) -> tuple[list[dict], str]:
    """(eventi JSONL, righe non JSON). In output misto (stdout+stderr) le righe non JSON sono testo di errore/statistiche."""
    events, other = [], []
    for line in output.splitlines():
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("type"), str):  # una risposta -s può essere JSON
                events.append(data)
                continue
        if line.strip():
            other.append(line)
    return events, "\n".join(other)


def answer_text(events: list[dict], other: str = "") -> str:
    """Risposta finale: ultimo `assistant.message` con contenuto (preferendo phase final_answer); senza eventi, il testo (-s)."""
    messages = [e.get("data") or {} for e in events if e.get("type") == "assistant.message"]
    messages = [m for m in messages if str(m.get("content") or "").strip()]
    final = [m for m in messages if m.get("phase") == "final_answer"] or messages
    return str(final[-1]["content"]) if final else ("" if events else other)


def extract_json(text: str) -> dict | None:
    """Primo oggetto JSON nel testo, anche dentro ```json ... ``` o circondato da prosa."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _failure(code: int, events: list[dict], other: str) -> tuple[str | None, bool]:
    """(errore, quota esaurita) per un run fallito; (None, False) se è andato a buon fine."""
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    exit_code = result.get("exitCode", code)
    if code == 0 and not exit_code:
        return None, False
    detail = other.strip() or f"exit {code or exit_code}"
    if NOT_LOGGED_IN.search(detail):
        return t("prov.copilot_not_logged_in"), False
    if MODEL_UNAVAILABLE.search(detail):
        return f"copilot: {detail[:300]}", False
    return f"copilot: {tail(detail, 5)[:400]}", bool(QUOTA.search(detail) or LIMIT.search(detail))


class CopilotProvider(Provider):
    name = "copilot"
    binary = "copilot"
    # Niente "plan_structured" (copilot non ha --json-schema: lo schema lo mette il livello generico di #11, jarvis/providers/structured.py, non ancora presente: finché non c'è il planner non usa copilot)
    # e niente "quota" (nessun comando per le quote residue). "execute" e "review" sono costruiti sulle fixture della
    # spike (JSONL, errori); le modifiche ai file in headless non sono verificate con la CLI vera.
    capabilities = frozenset({"execute", "review"})

    def build_exec_cmd(self, executor: dict, prompt: str, *, commands: Sequence[str | None],
                       skip_permissions: bool, timeout_minutes: int) -> list[str]:
        # copilot in headless richiede un'approvazione degli strumenti. Senza skip_permissions: solo scritture e i comandi
        # permessi (euristica: l'allowlist shell(x:*) non è verificata in headless). Nessun flag di timeout: lo gestisce il runner.
        if skip_permissions:
            allow = ["--allow-all-tools"]
        else:
            allow = ["--allow-tool=write", *(f"--allow-tool=shell({c}:*)" for c in self.allowed_commands(commands, []))]
        return ["copilot", "-p", prompt, "--model", executor["model"], *COMMON, *allow, *DENY]

    def build_plan_cmd(self, executor: dict, prompt: str, *, timeout_minutes: int) -> list[str]:
        return ["copilot", "-p", prompt, "--model", executor["model"], *COMMON, *READ_ONLY]

    def build_review_cmd(self, reviewer: dict, prompt: str, *, schema: dict, timeout_minutes: int) -> list[str]:
        prompt = (f"{prompt}\n\nRispondi SOLO con un oggetto JSON valido che rispetta questo JSON schema, "
                  f"senza altro testo:\n{json.dumps(schema)}")
        return ["copilot", "-p", prompt, "--model", reviewer["model"], *COMMON, *READ_ONLY]

    def build_structured_cmd(self, model: str, system: str, message: str, schema: dict) -> list[str]:
        # Lo schema è già nel messaggio (livello generico). Nessun flag per il system prompt: va davanti al messaggio.
        # Nessun strumento in scrittura/shell, niente MCP integrato né file di istruzioni del progetto.
        prompt = f"{system}\n\n{message}" if system else message
        return ["copilot", "-p", prompt, "--model", model, *COMMON, "--allow-all-tools", "--deny-tool=write",
                "--deny-tool=shell", "--disable-builtin-mcps", "--no-custom-instructions"]

    def parse_structured(self, returncode: int, stdout: str, stderr: str) -> dict:
        events, other = parse_events(stdout)
        error, _ = _failure(returncode, events, other + "\n" + stderr)
        if error:
            raise ProviderError(error)
        data = extract_json(answer_text(events, other))
        if data is None:
            raise ProviderError(t("prov.copilot_no_json", code=returncode,
                                  tail=(answer_text(events, other) or stderr)[-300:]))
        return data

    def parse_output(self, code: int, output: str) -> tuple[str, str | None, bool]:
        if code == 124:
            return "", t("prov.agent_timeout"), False
        events, other = parse_events(output)
        summary = answer_text(events, other).strip()
        error, limited = _failure(code, events, other)
        return summary, error, limited

    def parse_review(self, code: int, output: str) -> tuple[dict | None, str | None, bool]:
        if code == 124:
            return None, t("prov.reviewer_timeout"), False
        events, other = parse_events(output)
        error, limited = _failure(code, events, other)
        if error:
            return None, error, limited
        review = extract_json(answer_text(events, other))
        if review is None and not events and not other:
            return None, t("prov.reviewer_unreadable", code=code, tail=tail(output, 3)), False
        if not isinstance(review, dict) or not isinstance(review.get("issues"), list):
            return None, t("prov.no_review"), False
        return review, None, False

    def event_lines(self, event: dict, root: str | None = None) -> list[str]:
        # Solo `assistant.message` (verificato). Forma di toolRequests ([{name, arguments}]) dedotta dall'SDK di
        # Copilot, non vista in una fixture con strumenti: euristica.
        if event.get("type") != "assistant.message":
            return []
        data = event.get("data") or {}
        lines = []
        for request in data.get("toolRequests") or []:
            if isinstance(request, dict):
                args = request.get("arguments")
                if line := tool_line(str(request.get("name") or "?"), args if isinstance(args, dict) else {}, root):
                    lines.append(line)
        if str(data.get("content") or "").strip():
            lines.insert(0, "💬 " + short(data["content"], root, 160))
        return lines

    def allowed_commands(self, commands: Sequence[str | None], submodule_paths: Sequence[str]) -> list[str]:
        base = [r[5:-3] for r in command_rules(*commands)]  # "Bash(x:*)" -> "x"
        return base + [f"git -C {path} {c}" for path in submodule_paths for c in SUBMODULE_GIT]
