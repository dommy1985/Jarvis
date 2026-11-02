"""Mailbox fra run in esecuzione insieme: gli agenti si scrivono con `jarvis-msg`, il runner fa da tramite.

L'agente non vede mai il token del runner: parla con un piccolo server HTTP su 127.0.0.1 (porta casuale) con un
token suo, valido solo finché il run è in corso. Il proxy impone il mittente (il run legato al token), toglie i
segreti dal testo e inoltra a jarvis (/runner/peers, /runner/message, /runner/inbox).

Quello che arriva da un altro agente è un dato, non un'istruzione: il proxy lo presenta sempre così.
"""

import json
import logging
import os
import re
import secrets
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jarvis import i18n
from jarvis.i18n import t as tr
from jarvis.redact import mask

log = logging.getLogger(__name__)

ASK_WAIT_SECONDS = 90  # un "ask" non blocca l'agente oltre questo: i tool Bash degli agenti hanno timeout brevi
ASK_POLL_SECONDS = 5
MAX_BODY = 16_000
FRAME_TAG = re.compile(r"<\s*/?\s*peer_message[^>]*>", re.IGNORECASE)  # il testo di un altro agente non chiude la cornice


class MailboxError(RuntimeError):
    """Rifiuto di jarvis (run non attivo, limite, attesa incrociata...): il messaggio va all'agente."""


SCRIPT = r'''#!/usr/bin/env python3
"""jarvis-msg: scrivi agli altri run Jarvis in esecuzione insieme a te.

  jarvis-msg peers                    gli altri run attivi
  jarvis-msg send <run> "testo"       una nota a un altro run
  jarvis-msg ask <run> "domanda"      una domanda: aspetta la risposta fino a ~90 s
  jarvis-msg reply <msg> "testo"      risponde a una domanda ricevuta
  jarvis-msg inbox                    i messaggi non ancora letti
"""
import json, os, sys, time, urllib.error, urllib.request

URL, TOKEN = os.environ.get("JARVIS_MSG_URL"), os.environ.get("JARVIS_MSG_TOKEN")
WAIT, POLL = __WAIT__, __POLL__
HOOK = "--hook" in sys.argv  # hook dopo ogni strumento: deve essere rapido, un tunnel lento non blocca l'agente


def call(path, body=None):
    req = urllib.request.Request(URL + path, data=json.dumps(body or {}).encode(),
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=4 if HOOK else 30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            error = json.load(e).get("error")
        except ValueError:
            error = str(e)
        raise SystemExit("jarvis-msg: " + str(error))
    except OSError as e:
        raise SystemExit("jarvis-msg: " + str(e))


def main(args):
    hook = "--hook" in args
    args = [a for a in args if a != "--hook"]
    if not URL or not TOKEN:
        if hook:
            return
        raise SystemExit("jarvis-msg: mailbox not available in this run")
    cmd = args[0] if args else "help"
    if cmd == "inbox":
        data = call("/inbox")
        if hook:  # hook PostToolUse di Claude: i messaggi entrano nel contesto, altrimenti niente
            if data["messages"]:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                         "additionalContext": data["text"]}}))
            return
        print(data["text"])
    elif cmd == "peers":
        print(call("/peers")["text"])
    elif cmd in ("send", "ask") and len(args) >= 3:
        sent = call("/send", {"to_run": int(args[1].lstrip("#")), "body": " ".join(args[2:]), "ask": cmd == "ask"})
        print(sent["text"])
        if cmd == "ask":
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline:
                time.sleep(POLL)
                data = call("/inbox")
                if data["messages"]:
                    print(data["text"])
                if any(m.get("reply_to") == sent["id"] for m in data["messages"]):
                    return
            print(sent["still_waiting"])
    elif cmd == "reply" and len(args) >= 3:
        print(call("/send", {"reply_to": int(args[1]), "body": " ".join(args[2:])})["text"])
    else:
        print(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SystemExit:
        if "--hook" in sys.argv:  # un hook che fallisce non deve disturbare l'agente
            sys.exit(0)
        raise
'''


def install_script(bin_dir: Path) -> Path:
    """Scrive `jarvis-msg` in `bin_dir` (una volta all'avvio del runner, in modo atomico)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "jarvis-msg"
    tmp = bin_dir / f".jarvis-msg.{os.getpid()}"
    tmp.write_text(SCRIPT.replace("__WAIT__", str(ASK_WAIT_SECONDS)).replace("__POLL__", str(ASK_POLL_SECONDS)))
    tmp.chmod(0o755)
    os.replace(tmp, target)
    return target


class Mailbox:
    """Proxy locale fra gli agenti e jarvis. `send(path, body)` inoltra a jarvis (Api.mailbox)."""

    def __init__(self, send, host: str = "127.0.0.1", port: int = 0):
        self.send = send
        self.tokens: dict[str, dict] = {}
        self.lock = threading.Lock()
        mailbox = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # niente righe su stderr per ogni chiamata
                pass

            def do_POST(self):
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(min(length, MAX_BODY)) or b"{}") if length <= MAX_BODY else None
                except ValueError:
                    body = None
                code, data = (413, {"error": "too large"}) if body is None else mailbox.handle(token, self.path, body)
                raw = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.url = f"http://{host}:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, name="mailbox", daemon=True).start()

    def register(self, run_id: int, locale: str, owner: str) -> dict:
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.tokens[token] = {"run_id": run_id, "locale": locale, "owner": owner}
        return {"url": self.url, "token": token, "run_id": run_id}

    def revoke(self, token: str) -> None:
        with self.lock:
            self.tokens.pop(token, None)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def handle(self, token: str, path: str, body: dict) -> tuple[int, dict]:
        with self.lock:
            who = self.tokens.get(token)
        if who is None:
            return 401, {"error": "unknown or expired token"}
        with i18n.using(who["locale"]):
            try:
                return 200, self._handle(who, path, body)
            except MailboxError as e:
                return 409, {"error": str(e)}
            except (KeyError, TypeError, ValueError) as e:
                return 400, {"error": str(e)}
            except (urllib.error.URLError, OSError) as e:
                return 503, {"error": tr("runner.msg.unreachable", error=e)}

    def _handle(self, who: dict, path: str, body: dict) -> dict:
        run_id = who["run_id"]
        if path == "/peers":
            peers = self.send("/runner/peers", {"run_id": run_id})["peers"]
            lines = [tr("runner.msg.peer", run_id=p["id"], project=p["project"] or "-", ref=p["external_id"],
                        title=p["title"]) for p in peers]
            return {"peers": peers, "text": "\n".join(lines) or tr("runner.msg.no_peers")}
        if path == "/send":
            payload = {"from_run": run_id, "body": mask(str(body.get("body") or "")), "ask": bool(body.get("ask"))}
            if body.get("reply_to") is not None:
                payload["reply_to"] = int(body["reply_to"])
            if body.get("to_run") is not None:
                payload["to_run"] = int(body["to_run"])
            msg_id = self.send("/runner/message", payload)["id"]
            return {"id": msg_id, "text": tr("runner.msg.sent", msg_id=msg_id),
                    "still_waiting": tr("runner.msg.still_waiting", msg_id=msg_id)}
        if path == "/inbox":
            messages = self.send("/runner/inbox", {"run_id": run_id})["messages"]
            return {"messages": messages, "text": render(messages, who["owner"])}
        raise ValueError(f"unknown path {path}")


def render(messages: list[dict], owner: str) -> str:
    """I messaggi ricevuti, sempre incorniciati come dati non fidati (mai istruzioni dell'owner)."""
    if not messages:
        return tr("runner.msg.empty")
    lines = [tr("runner.msg.untrusted", owner=owner)]
    for m in messages:
        tag = (tr("runner.msg.tag_ask", msg_id=m["id"]) if m["kind"] == "ask"
               else tr("runner.msg.tag_reply", reply_to=m["reply_to"]) if m.get("reply_to") else "")
        lines.append(tr("runner.msg.item", msg_id=m["id"], run_id=m["from_run"], tag=tag)
                     + "\n<peer_message>\n" + FRAME_TAG.sub("", m["body"]) + "\n</peer_message>")
    return "\n".join(lines)
