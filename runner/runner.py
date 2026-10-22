"""Runner Jarvis sul Mac (fase 2): prende un run dalla coda sul VPS, lo esegue in un worktree, apre la PR draft.

Il Mac non è esposto: il runner raggiunge jarvis con un tunnel SSH (127.0.0.1:8090 sul VPS) e chiede
lavoro con POST /runner/claim. Un run alla volta:

  worktree (agent/<id> da origin/<default>) -> copia file locali -> setup -> agente (claude o agy)
  -> build e test -> commit -> push del branch -> PR draft con gh -> rimozione del worktree

Fase 3: dopo build e test il diff passa alla controparte (altra famiglia) per una review in sola lettura;
se trova problemi gravi l'esecutore li corregge una volta e build e test ripartono. La review finisce nella
PR. Prima di lanciare agy (esecutore o revisore) il runner legge le quote con /usage. Un run 'realign'
(niente agente) sposta il submodule della PR app sul commit mergiato del framework.

Progetti con submodule (my-app): se l'agente cambia un submodule, il runner apre prima una PR
sul repo del submodule (branch agent/<id> dal commit puntato dall'app), poi quella dell'app con il
puntatore aggiornato. I task del submodule girano nel worktree dell'app, così si provano nell'app.
Durante i passi lunghi il runner chiede a jarvis ogni 30 secondi se il run va fermato ("ferma N").

Se un passo fallisce il worktree resta sul disco per guardarci dentro, e il run torna su Telegram come fallito.
Uso: uv run python -m runner.runner [--once]
"""

import argparse
import functools
import json
import logging
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from jarvis import i18n, providers
from jarvis.i18n import t as tr
from jarvis.providers.base import QUOTA, last_result, tail  # noqa: F401 (tail, QUOTA: usati anche dai test)
from jarvis.providers.claude import CLAUDE_LIMIT, RESET_EPOCH, RESET_TIME, claude_reset, command_rules  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
# Prompt in prompts/<locale>/: task, task-suffix (solo Claude: la review finale la fa Jarvis), review, fix, plan,
# plan-revise, approved-plan. Il locale è quello di job["locale"] (vedi job_locale).
LEGACY_LOCALE = "it"  # un server precedente all'i18n non manda job["locale"] e parlava sempre italiano
LOG_FLUSH_SECONDS = 5  # righe di log verso jarvis: a gruppi
SECRET = re.compile(r"ghp_\w{16,}|github_pat_\w{20,}|sk-[\w-]{20,}|AIza[\w-]{30,}|Bearer\s+\S+|"
                    r"(?i:(?:token|password|passwd|secret|api[_-]?key)\s*[=:]\s*)\S+")
MAX_DIFF = 80_000  # caratteri di diff passati al revisore
SERIOUS = ("blocker", "major")  # severità che fanno partire il giro di correzione
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "changes"]},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
            "file": {"type": "string"},
            "problem": {"type": "string"},
            "fix": {"type": "string"},
        }, "required": ["severity", "problem"]}},
    },
    "required": ["verdict", "summary", "issues"],
}
ENV_FILE = Path("~/.config/jarvis/runner.env").expanduser()
JARVIS_HOME = Path(os.environ.get("JARVIS_HOME") or "~/.jarvis").expanduser()  # stato locale del runner
PENDING = JARVIS_HOME / "pending-updates.jsonl"  # esiti finali non ancora consegnati
TRANSIENT = re.compile(r"timed? ?out|timeout|connection reset|UNAVAILABLE|\b50[234]\b|overloaded", re.IGNORECASE)
LOCAL_PORT = 8090
POLL_SECONDS = 60
AGENT_RETRY_SECONDS = 30
CHECK_SECONDS = 30
HOOKS = JARVIS_HOME / "hooks"
TAIL = 40
DEFAULT_OWNER = "Owner"  # un server vecchio non manda job["owner"]
SECRETS_HINT = re.compile(r"(^|/)(\.env(\.[\w.-]+)?|local\.properties|google-services\.json|GoogleService-Info\.plist|"
                          r"key\.properties|[^/]+\.(keystore|jks|p12|pem))$")

log = logging.getLogger("runner")


def job_locale(job: dict) -> str:
    """Lingua di prompt, PR e messaggi del run. Senza job["locale"] il server è precedente all'i18n: italiano, come
    sempre. Un locale che questo runner non ha (server più nuovo) ripiega sull'inglese."""
    code = job.get("locale")
    if not code:
        return LEGACY_LOCALE
    return code if code in i18n.available() else i18n.DEFAULT


def localized(fn):
    """Esegue `fn(job, ...)` nel locale del job."""
    @functools.wraps(fn)
    def wrapper(job, *args, **kwargs):
        with i18n.using(job_locale(job)):
            return fn(job, *args, **kwargs)
    return wrapper


class Cancelled(RuntimeError):
    """Run fermato da Telegram."""


class StepError(RuntimeError):
    def __init__(self, step: str, message: str, rate_limited: bool = False, summary: str | None = None):
        super().__init__(message)
        self.step, self.rate_limited, self.summary = step, rate_limited, summary


# --- ambiente ---

def load_env(path: Path = ENV_FILE) -> dict:
    """runner.env: JARVIS_RUNNER_TOKEN (obbligatorio) e il collegamento al server.

    Un solo host: JARVIS_URL (default http://127.0.0.1:8090), nessun tunnel.
    Host separati: JARVIS_SSH_HOST apre `ssh -L <locale>:127.0.0.1:<remota> <host>`; porte in
    JARVIS_SSH_LOCAL_PORT (default: la porta di JARVIS_URL, altrimenti 8090) e JARVIS_SSH_REMOTE_PORT (8090).
    """
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    values.update({k: v for k, v in os.environ.items() if k.startswith("JARVIS_")})
    if not values.get("JARVIS_RUNNER_TOKEN"):
        raise SystemExit(f"JARVIS_RUNNER_TOKEN mancante in {path}")
    values["JARVIS_SSH_HOST"] = values.get("JARVIS_SSH_HOST", "").strip()
    url = values.get("JARVIS_URL", "").strip()
    try:
        url_port = urllib.parse.urlparse(url).port if url else None
        local = int(values.get("JARVIS_SSH_LOCAL_PORT") or url_port or LOCAL_PORT)
        remote = int(values.get("JARVIS_SSH_REMOTE_PORT") or LOCAL_PORT)
    except ValueError as e:
        raise SystemExit(f"porta non valida in {path}: {e}") from e
    values["JARVIS_SSH_LOCAL_PORT"], values["JARVIS_SSH_REMOTE_PORT"] = local, remote
    values["JARVIS_URL"] = url or f"http://127.0.0.1:{local}"
    return values


@functools.cache
def login_shell() -> str:
    """zsh sul Mac (com'è sempre stato), la shell di login di /etc/passwd su Linux; ripiega su zsh/bash/sh."""
    if sys.platform == "darwin":
        return "/bin/zsh"
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        shell = ""
    if not shell or not os.access(shell, os.X_OK):
        shell = next((c for c in ("/bin/zsh", "/bin/bash", "/bin/sh") if os.access(c, os.X_OK)), "/bin/sh")
    return shell


def login_env() -> None:
    """Sotto launchd/systemd l'ambiente è minimo: prende quello della shell di login (PATH, JAVA_HOME, ANDROID_HOME, ...)."""
    try:
        out = subprocess.run([login_shell(), "-lc", "env -0"], capture_output=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    for entry in out.decode(errors="replace").split("\0"):
        key, sep, value = entry.partition("=")
        if not sep or key in ("PWD", "OLDPWD", "SHLVL", "_"):
            continue
        if key == "PATH" or key not in os.environ:  # il PATH di login vince, il resto non sovrascrive
            os.environ[key] = value


def agent_env(hooks: Path = HOOKS) -> dict:
    """Ambiente per gli agenti: push, gh e credenziali git spenti (il push lo fa solo il runner).

    È una protezione contro gli errori, non un confine di sicurezza: un agente con la shell può
    comunque usare direttamente le chiavi dell'utente.
    """
    (hooks / "pre-push").parent.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    if not hook.exists():
        hook.write_text("#!/bin/sh\necho 'push disabilitato: lo fa il runner Jarvis' >&2\nexit 1\n")
        hook.chmod(0o755)
    gh_config = hooks.parent / "gh-empty"
    gh_config.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK")}
    env.update({
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(hooks),
        "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": "",
        "GIT_SSH_COMMAND": "sh -c 'echo \"rete git disabilitata dal runner Jarvis\" >&2; exit 1'",
        "GIT_TERMINAL_PROMPT": "0",
        # Claude: dopo ogni comando la shell torna nella root del worktree (niente "cd" che resta appiccicato).
        "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1",
        "GH_CONFIG_DIR": str(gh_config),
    })
    return env


# --- API jarvis ---

class Api:
    def __init__(self, url: str, token: str, pending: Path = PENDING, retry_seconds: float = 10):
        self.url, self.token = url.rstrip("/"), token
        self.pending, self.retry_seconds = pending, retry_seconds
        self.reconnect = None  # riapre il tunnel SSH prima di un nuovo tentativo

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=5) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def claim(self) -> dict | None:
        return self._post("/runner/claim", {})["run"]

    def heartbeat(self, run_id: int) -> bool:
        """Segnala che il run è vivo; True se l'owner ha chiesto di fermarlo."""
        try:
            return bool(self._post("/runner/update", {"run_id": run_id}).get("cancel"))
        except (urllib.error.URLError, OSError):
            if self.reconnect:
                self.reconnect()
            return False

    def log(self, run_id: int, lines: list[str]) -> None:
        """Righe di log del run: un solo tentativo, se non arrivano pazienza (non devono rallentare l'agente)."""
        try:
            self._post("/runner/update", {"run_id": run_id, "log": lines})
        except (urllib.error.URLError, OSError):
            pass

    def update(self, run_id: int, **fields) -> bool:
        """Non interrompe mai il lavoro: ritenta riaprendo il tunnel; un esito finale non consegnato
        finisce in PENDING e viene rimandato prima del prossimo claim."""
        body = {"run_id": run_id, **fields}
        final = fields.get("status") in ("pr_open", "done", "failed", "cancelled")
        attempts = 6 if final else 2
        for attempt in range(attempts):
            try:
                self._post("/runner/update", body)
                return True
            except urllib.error.HTTPError as e:  # rifiutato da jarvis: ritentare non serve
                log.error("update run %s rifiutato: HTTP %s", run_id, e.code)
                return False
            except (urllib.error.URLError, OSError) as e:
                log.warning("update run %s non inviato (tentativo %d): %s", run_id, attempt + 1, e)
            if attempt + 1 < attempts:
                time.sleep(self.retry_seconds * (attempt + 1))
                if self.reconnect:
                    self.reconnect()
        if final:
            self.pending.parent.mkdir(parents=True, exist_ok=True)
            with self.pending.open("a") as f:
                f.write(json.dumps(body) + "\n")
            log.error("esito del run %s salvato in %s, lo rimando appena jarvis risponde", run_id, self.pending)
        return False

    def flush_pending(self) -> None:
        if not self.pending.exists():
            return
        left = []
        for line in self.pending.read_text().splitlines():
            body = json.loads(line)
            try:
                self._post("/runner/update", body)
                log.info("esito del run %s consegnato", body["run_id"])
            except urllib.error.HTTPError as e:
                log.error("esito del run %s scartato: HTTP %s", body["run_id"], e.code)
            except (urllib.error.URLError, OSError):
                left.append(line)
        if left:
            self.pending.write_text("\n".join(left) + "\n")
        else:
            self.pending.unlink()


class Tunnel:
    """ssh -L verso il server, riaperto quando cade. Non serve se JARVIS_URL risponde già.
    Senza host (un solo host) non apre nulla: controlla solo che il server risponda."""

    def __init__(self, host: str, api: Api, local_port: int = LOCAL_PORT, remote_port: int = LOCAL_PORT):
        self.host, self.api, self.proc = host, api, None
        self.local_port, self.remote_port = local_port, remote_port

    def ensure(self) -> bool:
        if self.api.healthy():
            return True
        if not self.host:
            return False
        self.close()
        self.proc = subprocess.Popen(
            ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
             "-o", "BatchMode=yes", "-L", f"{self.local_port}:127.0.0.1:{self.remote_port}", self.host],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(20):
            time.sleep(0.5)
            if self.api.healthy():
                return True
            if self.proc.poll() is not None:
                log.error("tunnel SSH chiuso: %s", (self.proc.stderr.read() or "").strip()[-300:])
                return False
        return False

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None


# --- comandi ---

def _kill(proc: subprocess.Popen) -> None:
    """Termina il comando e tutti i suoi figli (gradle, emulatori, sotto-agenti)."""
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def sh(cmd: list[str] | str, cwd: Path, timeout: int, env: dict | None = None, check=None) -> tuple[int, str]:
    """Esegue un comando (stringa: shell di login, come nel terminale). Restituisce codice e output.

    `check()` viene chiamata ogni CHECK_SECONDS: se restituisce True il comando viene fermato (codice 130).
    """
    args = [login_shell(), "-lc", cmd] if isinstance(cmd, str) else cmd
    proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                            start_new_session=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=max(1, min(CHECK_SECONDS, deadline - time.monotonic())))
            return proc.returncode, out + err
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                code, note = 124, f"\n[timeout dopo {timeout}s]"
            elif check and check():
                code, note = 130, "\n[fermato da Telegram]"
            else:
                continue
        _kill(proc)
        out, err = proc.communicate()
        return code, out + err + note


def sh_stream(cmd: list[str] | str, cwd: Path, timeout: int, env: dict | None = None, check=None,
              on_line=None) -> tuple[int, str]:
    """Come sh(), ma passa a `on_line` ogni riga di stdout appena arriva (agenti in stream-json)."""
    args = [login_shell(), "-lc", cmd] if isinstance(cmd, str) else cmd
    proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                            start_new_session=True, bufsize=1)
    out: list[str] = []
    err: list[str] = []

    def pump(stream, sink, callback):
        for line in stream:
            sink.append(line)
            if callback:
                try:
                    callback(line.rstrip("\n"))
                except Exception:  # noqa: BLE001 - un errore nel log non deve fermare l'agente
                    log.exception("on_line fallita")

    threads = [threading.Thread(target=pump, args=(proc.stdout, out, on_line), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err, None), daemon=True)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    code, note = None, ""
    while code is None:
        try:
            proc.wait(timeout=max(1, min(CHECK_SECONDS, deadline - time.monotonic())))
            code = proc.returncode
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                code, note = 124, f"\n[timeout dopo {timeout}s]"
            elif check and check():
                code, note = 130, "\n[fermato da Telegram]"
    if proc.poll() is None:
        _kill(proc)
    for t in threads:
        t.join(timeout=5)
    return code, "".join(out) + "".join(err) + note


def git(cwd: Path, *args: str, step: str, timeout: int = 300) -> str:
    code, out = sh(["git", *args], cwd, timeout)
    if code != 0:
        raise StepError(step, f"git {' '.join(args)}: {tail(out, 10)}")
    return out.strip()


# --- log dal vivo: azioni dell'agente (mai il contenuto dei file) ---

def mask(text: str) -> str:
    return SECRET.sub("***", text)


class LogFeed:
    """Righe di log del run verso jarvis, a gruppi ogni LOG_FLUSH_SECONDS ("log" e "segui" su Telegram)."""

    def __init__(self, api, run_id: int, root: Path | None = None, flush_seconds: float = LOG_FLUSH_SECONDS):
        self.api, self.run_id, self.flush_seconds = api, run_id, flush_seconds
        self.root = str(root) if root else None
        self.lines: list[str] = []
        self.lock = threading.Lock()
        self.last = time.monotonic()

    def line(self, text: str) -> None:
        with self.lock:
            self.lines.append(mask(text)[:300])
            due = time.monotonic() - self.last >= self.flush_seconds or len(self.lines) >= 40
        if due:
            self.flush()

    def events(self, cli: str):
        """Callback per sh_stream: ogni riga JSON dell'agente diventa zero o più righe di log."""
        provider = providers.get(cli)
        convert = provider.event_lines if provider else (lambda event, root=None: [])

        def on_line(raw: str) -> None:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                return
            if isinstance(event, dict):
                for text in convert(event, self.root):
                    self.line(text)
        return on_line

    def take(self) -> list[str]:
        with self.lock:
            lines, self.lines = self.lines, []
            self.last = time.monotonic()
        return lines

    def flush(self) -> None:
        if lines := self.take():
            self.api.log(self.run_id, lines)


def agent_command(job: dict, prompt: str) -> list[str]:
    executor, project, limits = job["executor"], job["project"], job["limits"]
    if provider := providers.get(executor["cli"]):
        return provider.build_exec_cmd(
            executor, prompt, skip_permissions=bool(executor.get("skip_permissions")),
            timeout_minutes=limits["agent_timeout_minutes"],
            commands=[project["setup"], project["build"], project["test"],
                      *(sub.get("test") for sub in job.get("submodules") or [])])
    raise StepError("agent", tr("runner.unknown_cli", cli=executor["cli"]))


def transient(output: str) -> bool:
    """Errore temporaneo dell'agente (rete, servizio sovraccarico): vale un secondo tentativo."""
    for line in output.splitlines():
        if line.startswith("AGY_ERROR:"):
            try:
                if json.loads(line[10:]).get("retryable"):
                    return True
            except json.JSONDecodeError:
                pass
    return bool(TRANSIENT.search(output)) and not re.search(r"quota|usage limit|RESOURCE_EXHAUSTED", output, re.I)


def parse_agent(cli: str, code: int, output: str) -> tuple[str, str | None, bool]:
    """(riepilogo, errore bloccante o None, quota esaurita)."""
    if code == 124:
        return "", tr("prov.agent_timeout"), False
    if provider := providers.get(cli):
        return provider.parse_output(code, output)
    return "", tr("runner.unknown_cli", cli=cli), False


def rate_limit_event(cli: str, output: str) -> dict:
    """Evento per quota_events: jarvis non rilancia quel provider fino al reset (vedi runs.cli_exhausted)."""
    return {"cli": cli, "kind": "rate_limited", "reset_at": provider.quota_reset(output) if (provider := providers.get(cli)) else None}


def owner_name(job: dict) -> str:
    return job.get("owner") or DEFAULT_OWNER


@localized
def build_prompt(job: dict) -> str:
    task = {k: job["task"].get(k) for k in ("external_id", "url", "type", "title", "labels", "description")}
    project = job["project"]
    if (provider := providers.get(job["executor"]["cli"])) is None:
        raise StepError("agent", tr("runner.unknown_cli", cli=job["executor"]["cli"]))
    if job["executor"].get("skip_permissions"):
        commands = tr("runner.commands_skip_permissions")
    else:
        allowed = provider.allowed_commands(
            [project["setup"], project["build"], project["test"]], [sub["path"] for sub in job.get("submodules") or []])
        commands = tr("runner.commands_allowed", allowed=", ".join(f"`{c}`" for c in allowed))
    extra = []
    for sub in job.get("submodules") or []:
        extra.append(tr("runner.note_submodule", path=sub["path"])
                     + (tr("runner.note_submodule_test", path=sub["path"], test=sub["test"]) if sub.get("test") else ""))
    if job.get("focus"):
        extra.append(tr("runner.note_focus", focus=job["focus"]))
    if job.get("user_note"):
        extra.append(tr("runner.user_note", note=job["user_note"]))
    extra += project.get("agent_notes") or []
    suffix = i18n.prompt("task-suffix") if provider.uses_task_suffix else ""
    if job.get("approved_plan"):
        suffix += i18n.prompt("approved-plan").replace("{owner}", owner_name(job)).replace("{plan}", job["approved_plan"])
    return ((i18n.prompt("task") + suffix)
            .replace("{extra}", "".join(f"- {line}\n" for line in extra))
            .replace("{branch}", job["branch"])
            .replace("{task}", json.dumps(task, ensure_ascii=False, indent=1))
            .replace("{commands}", commands)
            .replace("{build}", project["build"] or tr("runner.none"))
            .replace("{test}", project["test"] or tr("runner.none")))


# --- quote, review incrociata ---

LOGS = JARVIS_HOME / "logs"  # output grezzo di agenti e revisori, per capire cosa è successo


def save_output(run_id: int, what: str, output: str) -> None:
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"run-{run_id}-{what}.txt").write_text(output)
    except OSError as e:
        log.warning("output %s del run %s non salvato: %s", what, run_id, e)


def failed_tests(root: Path, since: float, limit: int = 10) -> list[str]:
    """Test JUnit falliti (report XML scritti dopo `since`): "Classe.metodo: messaggio"."""
    import xml.etree.ElementTree as ET
    found = []
    for xml in sorted(root.glob("**/build/test-results/**/TEST-*.xml")):
        try:
            if xml.stat().st_mtime < since:
                continue
            for case in ET.parse(xml).getroot().iter("testcase"):
                failure = case.find("failure")
                if failure is None:
                    failure = case.find("error")
                if failure is not None:
                    text = (failure.get("message") or failure.text or "").strip().splitlines()
                    found.append(f"{case.get('classname')}.{case.get('name')}: {' '.join(text[:3])[:300]}")
        except (OSError, ET.ParseError):
            continue
        if len(found) >= limit:
            break
    return found[:limit]

def review_command(reviewer: dict, prompt: str, minutes: int) -> list[str]:
    """Revisore in sola lettura (come, lo decide il provider)."""
    if provider := providers.get(reviewer["cli"]):
        return provider.build_review_cmd(reviewer, prompt, schema=REVIEW_SCHEMA, timeout_minutes=minutes)
    raise StepError("review", tr("runner.unknown_cli", cli=reviewer["cli"]))


def parse_review(cli: str, code: int, output: str) -> tuple[dict | None, str | None, bool]:
    """(review strutturata, errore, quota esaurita)."""
    if provider := providers.get(cli):
        return provider.parse_review(code, output)
    return None, tr("runner.unknown_cli", cli=cli), False


def collect_diff(path: Path, base: str, copied: list[str], step: str) -> str:
    """Tutto il lavoro rispetto a `base` (commit dell'agente compresi), esclusi i file locali copiati."""
    git(path, "add", "-A", step=step)
    for name in copied:
        git(path, "reset", "-q", "--", name, step=step)
    return git(path, "diff", "--cached", base, step=step)


def worktree_state(path: Path) -> str:
    return "\n".join(sh(["git", *args], path, 60)[1] for args in (["status", "--porcelain"], ["diff"]))


def discard_unstaged(path: Path, copied: list[str]) -> None:
    """Annulla quello che il revisore ha cambiato: il lavoro dell'agente è già nell'index."""
    sh(["git", "checkout", "--", "."], path, 60)
    sh(["git", "clean", "-fdq", *(f"--exclude={name}" for name in copied)], path, 60)


def review_section(review: dict) -> list[str]:
    lines = ["", tr("runner.review.head", reviewer=review["reviewer"]), ""]
    if review.get("error"):
        return lines + [tr("runner.review.not_run", error=review["error"])]
    lines.append(review.get("summary") or (tr("runner.review.no_issues") if not review.get("issues") else ""))
    for i in review.get("issues") or []:
        where = f"`{i['file']}`: " if i.get("file") else ""
        fix = f" → {i['fix']}" if i.get("fix") else ""
        lines.append(f"- **{i.get('severity', '?')}** {where}{i.get('problem', '')}{fix}")
    if review.get("rounds"):
        lines += ["", tr("runner.review.fixes_head"), "", review.get("fix_summary") or tr("runner.pr.no_summary")]
    elif review.get("fix_error"):
        lines += ["", tr("runner.review.fix_failed", error=review["fix_error"])]
    return lines


def pr_feedback_prompt(feedback: dict | None) -> dict:
    """Commenti delle PR e check falliti nel formato del blocco <review> di prompts/<locale>/fix.md. Le chiavi del
    JSON (`runner.fb.*`) sono quelle che il prompt cita per nome."""
    if not feedback:
        return {}
    out, k = {}, lambda name: tr("runner.fb." + name)  # noqa: E731
    if feedback.get("comments"):
        out[k("pr_comments")] = [{key: v for key, v in ((k("author"), c["author"]), (k("file"), c.get("file")),
                                                          (k("pr"), c["pr"]), (k("text"), c["text"])) if v}
                                 for c in feedback["comments"]]
    if feedback.get("ci"):
        out[k("failed_ci")] = [{key: v for key, v in ((k("check"), c["name"]), (k("result"), c["conclusion"]),
                                                        (k("pr"), c["pr"]), (k("details"), c.get("summary"))) if v}
                               for c in feedback["ci"]]
    return out


@localized
def repair_comment(job: dict, tests: str, test_output: str, notes: list[str], review: dict | None,
                   summary: str, pushed: bool) -> str:
    """Commento sulle PR dopo "ripara N": esito dei test, review e correzioni."""
    executor = job["executor"]
    lines = [tr("runner.repair.head", marker=tr("marker.repair_comment"), run_id=job["run_id"],
                executor=executor["name"]), "",
             tr("runner.repair.pushed" if pushed else "runner.repair.not_pushed"), ""]
    feedback = (job.get("repair") or {}).get("feedback") or {}
    if feedback.get("comments") or feedback.get("ci"):
        checks = sorted({c["name"] for c in feedback.get("ci") or []})
        lines += [tr("runner.repair.passed_to_agent", count=len(feedback.get("comments") or []))
                  + (tr("runner.repair.failed_checks", checks=", ".join(f"`{c}`" for c in checks)) if checks else "")
                  + ".", ""]
    if tests == "skipped":
        lines.append(tr("runner.pr.tests_skipped"))
    else:
        lines.append(tr("runner.repair.tests", result=tr("runner.pr.passed") if tests == "passed"
                        else tr("runner.repair.still_failed")))
        if tests == "failed":
            lines += ["", tr("runner.pr.last_lines"), "", "```", test_output, "```", "</details>"]
    if summary.strip():
        lines += ["", summary.strip()]
    if review:
        lines += review_section(review)
    if notes:
        lines += ["", tr("runner.pr.notes_head"), "", *(f"- {n}" for n in notes)]
    return "\n".join(lines)


def remove_worktree(repo: Path, wt: Path) -> None:
    """git rifiuta di rimuovere worktree con submodule: in quel caso si cancella la cartella e si fa prune."""
    if sh(["git", "worktree", "remove", "--force", str(wt)], repo, 120)[0] != 0:
        shutil.rmtree(wt, ignore_errors=True)
        sh(["git", "worktree", "prune"], repo, 60)


def default_base(repo: Path, step: str) -> str:
    """origin/<branch di default> del repo (es. origin/develop)."""
    try:
        return git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", step=step)
    except StepError:
        git(repo, "remote", "set-head", "origin", "--auto", step=step)
        return git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", step=step)


BASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def resolve_base(repo: Path, requested: str | None, step: str) -> str:
    """origin/<requested> se il run (o il progetto) chiede una base; altrimenti il branch di default del repo.

    Il fetch è già stato fatto: se il branch non esiste su origin il run fallisce subito, con un messaggio chiaro."""
    if not requested:
        return default_base(repo, step)
    if not BASE_NAME.fullmatch(requested) or ".." in requested or requested.endswith(("/", ".lock")):
        raise StepError(step, tr("runner.err.base_invalid", name=requested))
    ref = f"origin/{requested}"
    try:
        git(repo, "rev-parse", "--verify", "-q", f"refs/remotes/{ref}^{{commit}}", step=step)
    except StepError:
        raise StepError(step, tr("runner.err.base_missing", name=requested)) from None
    return ref


def copy_local(src_root: Path, dst_root: Path, names: list[str], notes: list[str], label: str = "") -> list[str]:
    copied = []
    for name in names or []:
        src, dst = src_root / name, dst_root / name
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(name)
        else:
            notes.append(tr("runner.note.local_missing", label=label, name=name))
    return copied


def commit_all(path: Path, copied: list[str], message: tuple[str, str], step: str) -> None:
    """Commit di tutto il lavoro nel repo `path`, esclusi i file locali copiati e con un controllo sui segreti."""
    git(path, "add", "-A", step=step)
    for name in copied:  # i file locali copiati non finiscono mai nel commit
        git(path, "reset", "-q", "--", name, step=step)
    staged = git(path, "diff", "--cached", "--name-only", step=step).splitlines()
    suspicious = [f for f in staged if SECRETS_HINT.search(f)]
    if suspicious:
        raise StepError(step, tr("runner.err.secrets", files=", ".join(suspicious)))
    if staged:
        git(path, "commit", "-q", "-m", message[0], "-m", message[1], step=step)


def create_pr(gh, cwd: Path, base: str, branch: str, title: str, body: str, step: str) -> str:
    code, out = gh(["gh", "pr", "create", "--draft", "--base", base.split("/", 1)[1], "--head", branch,
                    "--title", title, "--body", body], cwd, 120)
    urls = re.findall(r"https://github\.com/\S+/pull/\d+", out)
    if code != 0 or not urls:
        raise StepError(step, tr("runner.err.pr_create", code=code, tail=tail(out, 5)))
    return urls[-1]


@localized
def pr_body(job: dict, summary: str, tests: str, test_output: str, notes: list[str],
            repo: str | None = None, links: list[str] = (), review: dict | None = None) -> str:
    """Descrizione della PR sul repo `repo` (None: quello del progetto). `links`: righe sulle PR collegate."""
    task, executor = job["task"], job["executor"]
    lines = [tr("runner.pr.task", id=task["external_id"], url=task["url"], title=task["title"]),
             tr("runner.pr.executor", name=executor["name"], model=executor["model"], run_id=job["run_id"])
             + (tr("runner.pr.fallback", executor=job["fallback_from"]) if job.get("fallback_from") else ""), ""]
    if links:
        lines += [*links, ""]
    command = " && ".join(filter(None, [job["project"]["build"], job["project"]["test"]]))
    if tests == "skipped":
        lines.append(tr("runner.pr.tests_skipped"))
    else:
        lines += [tr("runner.pr.tests", command=command,
                     result=tr("runner.pr.passed") if tests == "passed" else tr("runner.pr.failed"))]
        if tests == "failed":
            lines += ["", tr("runner.pr.last_lines"), "", "```", test_output, "```", "</details>"]
    lines += ["", tr("runner.pr.summary_head"), "", summary.strip() or tr("runner.pr.no_summary")]
    if review:
        lines += review_section(review)
    if notes:
        lines += ["", tr("runner.pr.notes_head"), "", *(f"- {n}" for n in notes)]
    task_repo, _, number = task["external_id"].partition("#")
    if task["source"] == "github" and number and (repo is None or repo.lower() == task_repo.lower()):
        lines += ["", f"Closes {task_repo}#{number}"]
    lines += ["", "---", tr("runner.pr.footer")]
    return "\n".join(lines)


# --- esecuzione ---

@localized
def execute(job: dict, api: Api, run_agent=sh_stream, gh=sh, usage=None) -> None:
    run_id, project, limits = job["run_id"], job["project"], job["limits"]
    submodules = job.get("submodules") or []
    quota: list[dict] = []  # eventi per la tabella quota_events di jarvis
    feed = LogFeed(api, run_id, Path(limits["worktrees"]).expanduser() / f"{project['name']}-{run_id}")

    def finish(**fields) -> None:
        """Aggiornamento finale: porta con sé le ultime righe di log (dopo, jarvis non le accetta più)."""
        api.update(run_id, log=feed.take(), **fields)

    def stop_requested() -> bool:
        return api.heartbeat(run_id)

    def checkpoint() -> None:
        if stop_requested():
            raise Cancelled()

    def run_tests(command: str, cwd: Path) -> tuple[str, str]:
        code, out = sh(command, cwd, limits["test_timeout_minutes"] * 60, check=stop_requested)
        if code == 130:
            raise Cancelled()
        return ("passed" if code == 0 else "failed"), tail(out)

    def not_ready(who: dict) -> str | None:
        """CLI non installata su questa macchina: il run non parte e jarvis passa al fallback."""
        provider = providers.get(who["cli"])
        reason = provider.check_ready() if provider else tr("runner.unknown_cli", cli=who["cli"])
        if reason:
            quota.append({"cli": who["cli"], "kind": "rate_limited", "reset_at": None})  # evitata per un po'
        return reason

    def quota_ready(who: dict) -> str | None:
        """Per i provider con la capability "quota": legge le quote prima di partire. Restituisce il bucket
        esaurito, se c'è."""
        provider = providers.get(who["cli"])
        if provider is None or "quota" not in provider.capabilities:
            return None
        buckets = usage() if usage else provider.usage(sh)
        quota.extend(buckets)
        empty = provider.exhausted(who["model"], buckets)
        for b in empty:
            quota.append({**b, "kind": "exhausted"})
        return tr("runner.quota_detail", bucket=empty[0]["bucket"], reset=empty[0]["reset_at"]) if empty else None

    def run_executor(prompt: str, what: str = "agent") -> tuple[str, str | None, bool, int, str]:
        cli = job["executor"]["cli"]
        for attempt in (1, 2):
            code, out = run_agent(agent_command(job, prompt), cwd, limits["agent_timeout_minutes"] * 60 + 60,
                                  agent_env(), check=stop_requested, on_line=feed.events(cli))
            if code == 130:
                raise Cancelled()
            save_output(run_id, what, out)
            summary, error, rate_limited = parse_agent(cli, code, out)
            if not error or rate_limited or code == 124 or attempt == 2 or not transient(out):
                break
            log.warning("run %s: errore temporaneo dell'agente, riprovo: %s", run_id, error[:200])
            notes.append(tr("runner.note.transient", error=error[:200]))
            time.sleep(AGENT_RETRY_SECONDS)
        if rate_limited:
            quota.append(rate_limit_event(cli, out))
        return summary, error, rate_limited, code, out

    def changed_submodules() -> list[dict]:
        return [sub for sub in submodules
                if set(git(wt / sub["path"], "status", "--porcelain", step=step).splitlines()) - sub["baseline"]
                or git(wt / sub["path"], "rev-parse", "HEAD", step=step) != sub["pinned"]]

    def all_tests(changed: list[dict]) -> tuple[str, str, list[str], list[str]]:
        """(esito, ultime righe, note sui test dei submodule, test falliti letti dai report JUnit)."""
        start = time.time() - 5
        tests, output, sub_notes = "skipped", "", []
        feed.line(tr("runner.feed.tests"))
        command = " && ".join(filter(None, [project.get("build"), project.get("test")]))
        if command:
            api.update(run_id, step=step)
            tests, output = run_tests(command, cwd)
        for sub in changed:  # i test del submodule girano nel suo repo (formatKotlin compreso)
            if sub.get("test"):
                api.update(run_id, step=step)
                result, out = run_tests(sub["test"], wt / sub["path"])
                sub_notes.append(tr("runner.note.sub_tests", path=sub["path"], command=sub["test"],
                                    result=tr("runner.pr.passed") if result == "passed" else tr("runner.pr.failed")))
                if result == "failed":
                    tests, output = "failed", f"[{sub['path']}]\n{out}"
                elif tests == "skipped":
                    tests = "passed"
        failures = failed_tests(wt, start) if tests == "failed" else []
        if failures:
            output = tr("runner.failed_tests_head") + "\n" + "\n".join(f"- {f}" for f in failures) + "\n\n" + output
        return tests, output, sub_notes, failures

    def cross_review(changed: list[dict], tests: str, failures: list[str]) -> dict | None:
        """Review del diff dalla controparte, in sola lettura. Non blocca mai la PR."""
        reviewer = job.get("reviewer")
        if not reviewer:
            return None
        result = {"reviewer": reviewer["name"], "rounds": 0}
        feed.line(tr("runner.feed.review", name=reviewer["name"]))
        empty = quota_ready(reviewer)
        if empty:
            return {**result, "error": tr("runner.err.review_quota", name=reviewer["name"], detail=empty)}
        diff = collect_diff(wt, base, copied, step)
        for sub in changed:
            sub_diff = collect_diff(wt / sub["path"], sub.get("review_base") or sub["pinned"], sub["copied"], step)
            diff += f"\n\n# submodule {sub['path']}\n{sub_diff}"
        if len(diff) > MAX_DIFF:
            diff = diff[:MAX_DIFF] + tr("runner.diff_truncated")
        task = {k: job["task"].get(k) for k in ("external_id", "url", "type", "title", "description")}
        prompt = (i18n.prompt("review").replace("{branch}", branch)
                  .replace("{task}", json.dumps(task, ensure_ascii=False, indent=1))
                  .replace("{tests}", tr("runner.tests_" + (tests if tests in ("passed", "failed") else "skipped"))
                           + "".join(f"\n- {f}" for f in failures))
                  .replace("{diff}", diff))
        paths = [(wt, copied), *((wt / sub["path"], sub["copied"]) for sub in changed)]
        before = [worktree_state(path) for path, _ in paths]
        for attempt in (1, 2):
            minutes = int(job.get("review", {}).get("timeout_minutes", 15))
            code, out = run_agent(review_command(reviewer, prompt, minutes), wt, minutes * 60 + 30, agent_env(),
                                  check=stop_requested)
            if code == 130:
                raise Cancelled()
            save_output(run_id, f"review-{attempt}", out)
            for (path, names), state in zip(paths, before):
                if worktree_state(path) != state:
                    discard_unstaged(path, names)
                    notes.append(tr("runner.note.reviewer_edited", name=path.name))
            review, error, rate_limited = parse_review(reviewer["cli"], code, out)
            # agy in headless chiude il turno al primo comando negato, senza review: un secondo tentativo
            if not error or rate_limited or attempt == 2 or '"denied_actions"' not in out:
                break
            log.warning("run %s: revisore fermato su un comando negato, riprovo", run_id)
            prompt += tr("runner.review_retry")
        if rate_limited:
            quota.append(rate_limit_event(reviewer["cli"], out))
        return {**result, "error": error} if error else {**result, **review}

    repo = Path(project["path"]).expanduser()
    repair = job.get("repair")  # "ripara N": riparte dai branch delle PR del run N, senza rifare il lavoro
    branch = repair["branch"] if repair else job["branch"]
    wt = Path(limits["worktrees"]).expanduser() / f"{project['name']}-{run_id}"
    notes: list[str] = []
    step = "worktree"
    try:
        if not branch.startswith("agent/"):
            raise StepError(step, tr("runner.err.branch", branch=branch))
        git(repo, "fetch", "origin", "--prune", step=step)
        base = resolve_base(repo, job.get("base_branch"), step)
        wt.parent.mkdir(parents=True, exist_ok=True)
        if repair:  # staccato sul branch della PR: il push va su origin/<branch>, niente branch locale
            git(repo, "worktree", "add", "--detach", str(wt), f"origin/{branch}", step=step)
        else:
            git(repo, "worktree", "add", "-b", branch, str(wt), base, step=step)
        api.update(run_id, step=step, branch=branch, worktree_path=str(wt))
        copied = copy_local(repo, wt, project.get("copy_files"), notes)
        cwd = wt / project["workdir"] if project.get("workdir") else wt

        step = "setup"
        if project.get("setup"):
            api.update(run_id, step=step)
            code, out = sh(project["setup"], cwd, limits["setup_timeout_minutes"] * 60, check=stop_requested)
            if code == 130:
                raise Cancelled()
            if code != 0:
                raise StepError(step, tr("runner.err.setup", command=project["setup"], code=code, tail=tail(out, 10)))
        for sub in submodules:
            path = wt / sub["path"]
            if not (path / ".git").exists():
                raise StepError(step, tr("runner.err.submodule_missing", path=sub["path"]))
            sub["copied"] = copy_local(Path(sub["local_path"]).expanduser(), path, sub.get("copy_files"), notes,
                                       f"{sub['path']}/")
            sub["pinned"] = git(path, "rev-parse", "HEAD", step=step)
            # La review guarda tutto il lavoro del task: nel submodule parte dal commit puntato dall'app su base.
            sub["review_base"] = git(wt, "rev-parse", f"{base}:{sub['path']}", step=step) if repair else sub["pinned"]
            sub["pr"] = next((u for u in (repair or {}).get("sub_pr_urls") or [] if sub["repo"] and
                              f"github.com/{sub['repo'].lower()}/pull/" in u.lower()), None)
            sub["baseline"] = set(git(path, "status", "--porcelain", step=step).splitlines())
        checkpoint()

        # Quello che setup e file copiati lasciano nel worktree non conta come lavoro dell'agente.
        baseline = set(git(wt, "status", "--porcelain", step=step).splitlines())
        if repair:
            summary = ""
        else:
            step = "agent"
            api.update(run_id, step=step)
            feed.line(tr("runner.feed.agent", name=job["executor"]["name"]))
            if reason := not_ready(job["executor"]):
                raise StepError(step, tr("runner.err.not_ready", cli=job["executor"]["cli"], reason=reason),
                                rate_limited=True)
            empty = quota_ready(job["executor"])
            if empty:
                raise StepError(step, tr("runner.err.quota", cli=job["executor"]["cli"], detail=empty),
                                rate_limited=True)
            summary, error, rate_limited, _, _ = run_executor(build_prompt(job))
            if error:
                raise StepError(step, error, rate_limited)
            dirty = set(git(wt, "status", "--porcelain", step=step).splitlines()) - baseline
            ahead = int(git(wt, "rev-list", "--count", f"{base}..HEAD", step=step))
            if not dirty and not ahead and not changed_submodules():
                raise StepError(step, tr("runner.err.no_change"), summary=summary.strip() or None)
        changed = changed_submodules()
        reviewed = [sub for sub in submodules if sub["pr"] or sub in changed]  # submodule nel lavoro del task
        checkpoint()

        fixes: list[str] = []  # riepiloghi dei giri di correzione

        def fix_round(review: dict | None, serious: list[dict], tests: str, test_output: str,
                      failures: list[str], pr: dict | None = None) -> bool:
            nonlocal changed
            checkpoint()
            api.update(run_id, step="fix")
            feed.line(tr("runner.feed.fix"))
            feedback = {"summary": (review or {}).get("summary") if serious else None, "issues": serious or None,
                        tr("runner.fb.failed_tests"): failures or ([tail(test_output, 30)] if tests == "failed" else None),
                        **pr_feedback_prompt(pr)}
            fix_summary, error, _, _, _ = run_executor(
                build_prompt(job) + i18n.prompt("fix").replace(
                    "{review}", json.dumps({k: v for k, v in feedback.items() if v}, ensure_ascii=False, indent=1)),
                f"fix-{len(fixes) + 1}")
            if error:
                notes.append(tr("runner.note.fix_failed", error=error[:300]))
                return False
            fixes.append(fix_summary.strip())
            changed = [sub for sub in submodules if sub in changed or sub in changed_submodules()]
            return True

        step = "tests"
        tests, test_output, sub_notes, failures = all_tests(reviewed)
        max_fixes = 2 if repair else job.get("review", {}).get("fix_rounds", 1)
        pr = (repair or {}).get("feedback")  # commenti di review e CI rossa sulle PR
        if repair and (tests == "failed" or pr) and max_fixes and fix_round(None, [], tests, test_output, failures, pr):
            step = "tests"  # riparazione: prima test e commenti, poi la review sul lavoro corretto
            reviewed = [sub for sub in submodules if sub["pr"] or sub in changed]
            tests, test_output, sub_notes, failures = all_tests(reviewed)
        checkpoint()

        step = "review"
        api.update(run_id, step=step)
        review = cross_review(reviewed, tests, failures)
        serious = [i for i in (review or {}).get("issues") or [] if i.get("severity") in SERIOUS]
        # Giro di correzione: problemi gravi della review o test falliti (anche senza revisore).
        if (serious or tests == "failed") and len(fixes) < max_fixes:
            step = "fix"
            if fix_round(review, serious, tests, test_output, failures):
                step = "tests"
                reviewed = [sub for sub in submodules if sub["pr"] or sub in changed]
                tests, test_output, sub_notes, failures = all_tests(reviewed)
        fix_text = "\n\n".join(fixes)
        if review is not None and fixes:
            review.update(rounds=len(fixes), fix_summary=fix_text)
        elif fixes:
            summary += "\n\n" + tr("runner.fix_after_tests") + "\n\n" + fix_text
        notes += sub_notes
        checkpoint()

        step = "push"
        api.update(run_id, step=step)
        feed.line(tr("runner.feed.push"))
        task = job["task"]
        message = (f"{task['external_id']}: {task['title']}"[:200] if not repair else
                   tr("runner.commit_title_fix", id=task["external_id"])[:200],
                   tr("runner.commit_body", run_id=run_id, executor=job["executor"]["name"]))
        for sub in changed:  # prima i submodule: il commit dell'app punta ai loro commit
            path = wt / sub["path"]
            if not repair:
                git(path, "checkout", "-q", "-B", branch, step=step)
            commit_all(path, sub["copied"], message, step)
            if git(path, "rev-parse", "HEAD", step=step) == sub["pinned"]:
                if repair:
                    continue
                raise StepError(step, tr("runner.err.nothing_in_sub", path=sub["path"]))
            sub["base"] = default_base(path, step)
            git(path, "push", "-u", "origin", f"HEAD:refs/heads/{branch}", step=step, timeout=600)
        commit_all(wt, copied, message, step)
        if repair:
            pushed = int(git(wt, "rev-list", "--count", f"origin/{branch}..HEAD", step=step)) > 0
            if pushed:
                git(wt, "push", "origin", f"HEAD:refs/heads/{branch}", step=step, timeout=600)
        else:
            if int(git(wt, "rev-list", "--count", f"{base}..HEAD", step=step)) == 0:
                raise StepError(step, tr("runner.err.nothing"))
            git(wt, "push", "-u", "origin", branch, step=step, timeout=600)

        step = "pr"
        api.update(run_id, step=step)
        feed.line(tr("runner.feed.pr"))
        title = f"{task['external_id'].split('/')[-1]} {task['title']}"[:240]
        merge_note = tr("runner.merge_note", branch=branch)
        if repair:
            comment = repair_comment(job, tests, test_output, notes, review, summary, pushed)
            for url in [*(sub["pr"] for sub in submodules if sub["pr"]), repair["pr_url"]]:
                code, out = gh(["gh", "pr", "comment", url, "--body", comment], wt, 120)
                if code != 0:
                    log.warning("run %s: commento non aggiunto in %s: %s", run_id, url, tail(out, 3))
            new = [sub for sub in changed if not sub["pr"]]  # submodule toccato solo ora: serve la sua PR
            for sub in new:
                body = pr_body(job, summary, tests, test_output, notes, repo=sub.get("repo"),
                               links=[tr("runner.app_pr_link", url=repair["pr_url"])], review=review)
                sub["pr"] = create_pr(gh, wt / sub["path"], sub["base"], branch, title, body, step)
                gh(["gh", "pr", "comment", repair["pr_url"], "--body",
                    tr("runner.sub_pr_link", path=sub["path"], url=sub["pr"]) + f"\n\n{merge_note}"], wt, 120)
            finish(status="done", step=step, pr_url=repair["pr_url"], tests=tests,
                   sub_pr_urls=[sub["pr"] for sub in submodules if sub["pr"]], summary=summary[:4000] or None,
                   review=review, quota=quota)
            log.info("run %s: PR del run %s aggiornate", run_id, repair["parent_run_id"])
            remove_worktree(repo, wt)
            return
        sub_prs = []
        for sub in changed:
            body = pr_body(job, summary, tests, test_output, notes, repo=sub.get("repo"),
                           links=[tr("runner.app_pr_pending")], review=review)
            sub["body"], sub["pr"] = body, create_pr(gh, wt / sub["path"], sub["base"], branch, title, body, step)
            sub_prs.append(sub["pr"])
        links = [tr("runner.sub_pr_link", path=sub["path"], url=sub["pr"]) for sub in changed]
        body = pr_body(job, summary, tests, test_output, notes, repo=job.get("repo"),
                       links=[*links, merge_note] if changed else [], review=review)
        pr_url = create_pr(gh, wt, base, branch, title, body, step)
        for sub in changed:  # la PR del submodule rimanda a quella dell'app
            body = sub["body"].replace(tr("runner.app_pr_pending"), tr("runner.app_pr_link", url=pr_url))
            code, out = gh(["gh", "pr", "edit", sub["pr"], "--body", body], wt / sub["path"], 120)
            if code != 0:
                log.warning("run %s: link alla PR app non aggiunto in %s: %s", run_id, sub["pr"], tail(out, 3))
        finish(status="pr_open", step=step, pr_url=pr_url, sub_pr_urls=sub_prs, tests=tests,
               summary=summary[:4000], review=review, quota=quota)
        log.info("run %s: PR %s", run_id, " ".join([*sub_prs, pr_url]))
        remove_worktree(repo, wt)
    except Cancelled:
        log.info("run %s fermato da Telegram in %s", run_id, step)
        finish(status="cancelled", step=step)
        if wt.exists():  # fermato apposta: niente da guardare, il branch non è mai stato pushato
            remove_worktree(repo, wt)
            if not repair:
                sh(["git", "branch", "-D", branch], repo, 60)
    except StepError as e:
        log.error("run %s fallito in %s: %s", run_id, e.step, e)
        finish(status="failed", step=e.step, error=str(e), rate_limited=e.rate_limited,
               summary=e.summary[:4000] if e.summary else None, quota=quota)
    except Exception as e:  # noqa: BLE001 - il run non deve restare "running" per un bug del runner
        log.exception("run %s: errore inatteso", run_id)
        finish(status="failed", step=step, error=tr("runner.err.runner", error=e), quota=quota)


def plan_command(job: dict, prompt: str) -> list[str]:
    """Agente in sola lettura che scrive il piano (come, lo decide il provider)."""
    executor, minutes = job["executor"], int(job["plan"]["timeout_minutes"])
    if provider := providers.get(executor["cli"]):
        return provider.build_plan_cmd(executor, prompt, timeout_minutes=minutes)
    raise StepError("agent", tr("runner.unknown_cli", cli=executor["cli"]))


@localized
def plan_prompt(job: dict) -> str:
    task = {k: job["task"].get(k) for k in ("external_id", "url", "type", "title", "labels", "description")}
    extra = []
    for sub in job.get("submodules") or []:
        extra.append(tr("runner.note_submodule_plan", path=sub["path"]))
    if job.get("focus"):
        extra.append(tr("runner.note_focus", focus=job["focus"]))
    if job.get("user_note"):
        extra.append(tr("runner.user_note", note=job["user_note"]))
    extra += job["project"].get("agent_notes") or []
    prompt = (i18n.prompt("plan").replace("{owner}", owner_name(job)).replace("{extra}", "".join(f"- {line}\n" for line in extra))
              .replace("{task}", json.dumps(task, ensure_ascii=False, indent=1)))
    if job["plan"].get("previous"):
        prompt += (i18n.prompt("plan-revise").replace("{owner}", owner_name(job))
                   .replace("{previous}", job["plan"]["previous"])
                   .replace("{note}", job["plan"].get("note") or ""))
    return prompt


@localized
def execute_plan(job: dict, api: Api, run_agent=sh_stream, usage=None) -> None:
    """Run "plan" (`vai N piano`, `modifica N <testo>`): l'agente legge il codice e scrive il piano, niente viene
    modificato né pushato. Il piano torna a jarvis (campo `plan`) e arriva all'owner su Telegram."""
    run_id, project, limits = job["run_id"], job["project"], job["limits"]
    repo = Path(project["path"]).expanduser()
    wt = Path(limits["worktrees"]).expanduser() / f"{project['name']}-{run_id}"
    feed = LogFeed(api, run_id, wt)
    quota: list[dict] = []
    cli, minutes = job["executor"]["cli"], int(job["plan"]["timeout_minutes"])
    step = "worktree"

    def stop_requested() -> bool:
        return api.heartbeat(run_id)

    def finish(**fields) -> None:
        api.update(run_id, log=feed.take(), **fields)

    try:
        git(repo, "fetch", "origin", "--prune", step=step)
        base = resolve_base(repo, job.get("base_branch"), step)
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "--detach", str(wt), base, step=step)
        api.update(run_id, step=step, worktree_path=str(wt))
        copied = copy_local(repo, wt, project.get("copy_files"), [])
        cwd = wt / project["workdir"] if project.get("workdir") else wt

        step = "setup"
        if project.get("setup"):
            api.update(run_id, step=step)
            feed.line(tr("runner.feed.setup"))
            code, out = sh(project["setup"], cwd, limits["setup_timeout_minutes"] * 60, check=stop_requested)
            if code == 130:
                raise Cancelled()
            if code != 0:
                raise StepError(step, tr("runner.err.setup", command=project["setup"], code=code, tail=tail(out, 10)))
        guarded = [wt, *(wt / sub["path"] for sub in job.get("submodules") or [])]
        before = [worktree_state(path) for path in guarded]
        if stop_requested():
            raise Cancelled()

        step = "agent"
        api.update(run_id, step=step)
        feed.line(tr("runner.feed.plan", name=job["executor"]["name"]))
        provider = providers.get(cli)
        if provider and (reason := provider.check_ready()):
            quota.append({"cli": cli, "kind": "rate_limited", "reset_at": None})  # evitata per un po'
            raise StepError(step, tr("runner.err.not_ready", cli=cli, reason=reason), rate_limited=True)
        if provider and "quota" in provider.capabilities:
            buckets = usage() if usage else provider.usage(sh)
            quota.extend(buckets)
            empty = provider.exhausted(job["executor"]["model"], buckets)
            if empty:
                quota.extend({**b, "kind": "exhausted"} for b in empty)
                raise StepError(step, tr("runner.err.quota", cli=cli, detail=tr(
                    "runner.quota_detail", bucket=empty[0]["bucket"], reset=empty[0]["reset_at"])), rate_limited=True)
        code, out = run_agent(plan_command(job, plan_prompt(job)), cwd, minutes * 60 + 30, agent_env(),
                              check=stop_requested, on_line=feed.events(cli))
        if code == 130:
            raise Cancelled()
        save_output(run_id, "plan", out)
        plan, error, rate_limited = parse_agent(cli, code, out)
        if rate_limited:
            quota.append(rate_limit_event(cli, out))
        if error:
            raise StepError(step, error, rate_limited)
        plan = plan.strip()
        if not plan:
            raise StepError(step, tr("runner.err.no_plan"))
        for path, state in zip(guarded, before):  # il piano è in sola lettura: se qualcuno ha toccato, si annulla
            if worktree_state(path) != state:
                discard_unstaged(path, copied)
                plan += tr("runner.plan_note_reverted", name=path.name)
        finish(status="done", step=step, plan=plan[:12000], quota=quota)
        log.info("run %s: piano pronto (%d caratteri)", run_id, len(plan))
        remove_worktree(repo, wt)
    except Cancelled:
        log.info("run %s fermato da Telegram in %s", run_id, step)
        finish(status="cancelled", step=step)
        if wt.exists():
            remove_worktree(repo, wt)
    except StepError as e:
        log.error("run %s fallito in %s: %s", run_id, e.step, e)
        finish(status="failed", step=e.step, error=str(e), rate_limited=e.rate_limited, quota=quota,
               summary=e.summary[:4000] if e.summary else None)
    except Exception as e:  # noqa: BLE001
        log.exception("run %s: errore inatteso", run_id)
        finish(status="failed", step=step, error=tr("runner.err.runner", error=e), quota=quota)


@localized
def execute_realign(job: dict, api: Api) -> None:
    """Dopo lo squash merge della PR del submodule: porta il submodule della PR app sul commit mergiato."""
    run_id, project, limits, info = job["run_id"], job["project"], job["limits"], job["realign"]
    repo = Path(project["path"]).expanduser()
    wt = Path(limits["worktrees"]).expanduser() / f"{project['name']}-{run_id}"
    branch, path = info["branch"], info["path"]
    step = "worktree"
    try:
        if not branch.startswith("agent/"):
            raise StepError(step, tr("runner.err.branch", branch=branch))
        git(repo, "fetch", "origin", "--prune", step=step)
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "--detach", str(wt), f"origin/{branch}", step=step)
        api.update(run_id, step=step, branch=branch, worktree_path=str(wt))
        copy_local(repo, wt, project.get("copy_files"), [])
        step = "setup"
        if project.get("setup"):
            api.update(run_id, step=step)
            code, out = sh(project["setup"], wt, limits["setup_timeout_minutes"] * 60)
            if code != 0:
                raise StepError(step, tr("runner.err.setup", command=project["setup"], code=code, tail=tail(out, 10)))
        sub = wt / path
        git(sub, "fetch", "origin", info["sha"], step=step, timeout=600)
        git(sub, "checkout", "-q", "--detach", info["sha"], step=step)
        git(wt, "add", path, step=step)
        if not git(wt, "diff", "--cached", "--name-only", step=step):
            api.update(run_id, status="done", step=step, tests="skipped", pr_url=info["pr_url"],
                       summary=tr("runner.realign_noop"))
            remove_worktree(repo, wt)
            return
        step = "tests"
        api.update(run_id, step=step)
        tests = "skipped"
        command = " && ".join(filter(None, [project.get("build"), project.get("test")]))
        if command:
            code, _ = sh(command, wt, limits["test_timeout_minutes"] * 60)
            tests = "passed" if code == 0 else "failed"
        step = "push"
        api.update(run_id, step=step)
        git(wt, "commit", "-q", "-m", f"Realign {path} to merged {info['sha'][:10]}",
            "-m", tr("runner.realign_commit_body", pr=info["sub_pr"], run_id=run_id, parent=info["parent_run_id"]),
            step=step)
        git(wt, "push", "origin", f"HEAD:refs/heads/{branch}", step=step, timeout=600)
        api.update(run_id, status="done", step=step, tests=tests, pr_url=info["pr_url"])
        log.info("run %s: submodule %s riallineato a %s in %s", run_id, path, info["sha"][:10], branch)
        remove_worktree(repo, wt)
    except StepError as e:
        log.error("run %s fallito in %s: %s", run_id, e.step, e)
        api.update(run_id, status="failed", step=e.step, error=str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("run %s: errore inatteso", run_id)
        api.update(run_id, status="failed", step=step, error=tr("runner.err.runner", error=e))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Runner Jarvis")
    parser.add_argument("--once", action="store_true", help="prende al massimo un run ed esce")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = load_env()
    login_env()
    api = Api(env["JARVIS_URL"], env["JARVIS_RUNNER_TOKEN"])
    tunnel = Tunnel(env["JARVIS_SSH_HOST"], api, env["JARVIS_SSH_LOCAL_PORT"], env["JARVIS_SSH_REMOTE_PORT"])
    api.reconnect = tunnel.ensure
    signal.signal(signal.SIGTERM, lambda *_: (tunnel.close(), sys.exit(0)))
    log.info("runner avviato, jarvis su %s (%s)", env["JARVIS_URL"],
             f"tunnel ssh {env['JARVIS_SSH_HOST']}" if env["JARVIS_SSH_HOST"] else "connessione diretta")
    try:
        while True:
            job = None
            if tunnel.ensure():
                try:
                    api.flush_pending()
                    job = api.claim()
                except (urllib.error.URLError, OSError) as e:
                    log.error("claim: %s", e)
            if job:
                log.info("run %s: %s con %s", job["run_id"], job["task"]["external_id"], job["executor"]["name"])
                {"realign": execute_realign, "plan": execute_plan}.get(job.get("kind"), execute)(job, api)
            if args.once:
                break
            if not job:
                time.sleep(POLL_SECONDS)
    finally:
        tunnel.close()


if __name__ == "__main__":
    main()
