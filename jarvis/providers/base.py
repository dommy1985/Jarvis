"""Interfaccia comune dei provider: una CLI di modelli (claude, agy, copilot) dietro lo stesso contratto."""

import json
import re
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence

from jarvis.i18n import t

CAPABILITIES = ("execute", "plan_structured", "review", "quota")
QUOTA = re.compile(r"usage limit|rate.?limit|quota|RESOURCE_EXHAUSTED|\b429\b|limit reached|hit your .{0,20}limit",
                   re.IGNORECASE)


class ProviderError(RuntimeError):
    """Chiamata strutturata fallita (timeout, output illeggibile, errore del modello)."""


def tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def last_result(output: str) -> dict | None:
    """L'oggetto finale dell'agente: in stream-json l'evento `result` (claude: type, agy: event, con i campi della
    risposta dentro "result"), altrimenti l'ultima riga JSON (output json semplice)."""
    fallback = None
    for line in reversed(output.strip().splitlines()):
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("event") == "result" and isinstance(data.get("result"), dict):
            return data["result"]
        if data.get("type") == "result":
            return data
        fallback = fallback or data
    return fallback


class Provider(ABC):
    name: str                         # chiave `cli` in config/projects.yaml
    binary: str                       # eseguibile da cercare nel PATH
    capabilities: frozenset[str] = frozenset()
    uses_task_suffix = False          # prompts/task-suffix.md (advisor + review finale) solo per chi lo supporta

    def check_ready(self) -> str | None:
        """None se la CLI è installata; altrimenti il motivo. Il login non è verificabile senza una chiamata."""
        import shutil
        return None if shutil.which(self.binary) else t("prov.missing_binary", binary=self.binary)

    @abstractmethod
    def build_exec_cmd(self, executor: dict, prompt: str, *, commands: Sequence[str | None],
                       skip_permissions: bool, timeout_minutes: int) -> list[str]:
        """Agente che modifica il worktree. `commands`: comandi di setup/build/test del progetto (e dei submodule)."""

    @abstractmethod
    def build_plan_cmd(self, executor: dict, prompt: str, *, timeout_minutes: int) -> list[str]:
        """Agente in sola lettura che scrive un piano."""

    @abstractmethod
    def build_review_cmd(self, reviewer: dict, prompt: str, *, schema: dict, timeout_minutes: int) -> list[str]:
        """Revisore in sola lettura con output strutturato."""

    @abstractmethod
    def build_structured_cmd(self, model: str, system: str, message: str, schema: dict) -> list[str]:
        """Una sola risposta JSON secondo `schema`, senza strumenti (planner, comandi GitHub)."""

    @abstractmethod
    def parse_structured(self, returncode: int, stdout: str, stderr: str) -> dict:
        """Output di build_structured_cmd -> oggetto JSON. Solleva ProviderError."""

    @abstractmethod
    def parse_output(self, code: int, output: str) -> tuple[str, str | None, bool]:
        """Output di un run agente o piano -> (riepilogo, errore bloccante o None, quota esaurita)."""

    @abstractmethod
    def parse_review(self, code: int, output: str) -> tuple[dict | None, str | None, bool]:
        """Output del revisore -> (review strutturata, errore, quota esaurita)."""

    @abstractmethod
    def allowed_commands(self, commands: Sequence[str | None], submodule_paths: Sequence[str]) -> list[str]:
        """Comandi di shell che l'agente può lanciare (elenco mostrato nel prompt del task)."""

    def event_lines(self, event: dict, root: str | None = None) -> list[str]:
        """Righe di log dal vivo per un evento stream-json dell'agente."""
        return []

    def usage(self, run: Callable) -> list[dict]:
        """Capability "quota": snapshot delle quote residue (eventi 'snapshot'). `run(cmd, cwd, timeout)` -> (code, out)."""
        return []

    def exhausted(self, model: str, buckets: list[dict]) -> list[dict]:
        """Capability "quota": i bucket esauriti che riguardano `model`."""
        return []

    def quota_reset(self, output: str) -> str | None:
        """Orario di reset (ISO UTC) letto dal messaggio di quota esaurita, se il provider lo scrive."""
        return None


def run_structured(provider: Provider, system: str, message: str, schema: dict, model: str, timeout: int) -> dict:
    """Lancia la CLI senza strumenti né progetto (cwd temporaneo) e ne legge la risposta JSON."""
    cmd = provider.build_structured_cmd(model, system, message, schema)
    with tempfile.TemporaryDirectory() as cwd:  # nessun file di istruzioni del progetto da cui leggere
        try:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ProviderError(t("prov.timeout", name=provider.name, timeout=timeout)) from e
    return provider.parse_structured(proc.returncode, proc.stdout, proc.stderr)
