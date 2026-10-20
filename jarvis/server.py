"""API HTTP interna di Jarvis, raggiungibile solo dalla rete Docker (n8n).

GET  /health   stato del servizio
GET  /sources  query di ingest derivate da projects.yaml
POST /ingest   snapshot dei task aperti (vedi jarvis.ingest)
POST /plan     genera il briefing del giorno con il planner: {"date"?, "available_hours"?, "force"?, "if_missing"?}
               con if_missing un briefing già inviato non è un errore (message null) e un planner fallito
               risponde 200 con l'avviso da mandare su Telegram
GET  /briefing?date=YYYY-MM-DD  briefing con le voci
POST /briefing/sent  {"date", "message_id"}: il briefing è stato inviato su Telegram
POST /reply    messaggio Telegram del topic Jarvis: {"chat_id", "thread_id", "from_id", "message_id", "text"}
               "briefing" rimanda il briefing di oggi, "aiuto" l'elenco dei comandi,
               "vai N" mette in coda per il runner, "coda" ne mostra lo stato
POST /runner/claim   il runner sul Mac chiede il prossimo run: {"run": {...}} o {"run": null}
POST /runner/update  {"run_id", "status"?, "step"?, "pr_url"?, ...}: progressi ed esito del run

/plan e /reply restituiscono `message` ({chat_id, thread_id, text, reply_to_message_id}) da inviare
con parse_mode HTML, oppure null; `is_briefing` dice se dopo l'invio va chiamato /briefing/sent.

Tutte le rotte tranne /health richiedono `Authorization: Bearer $JARVIS_API_TOKEN`; quelle /runner/
solo `Bearer $JARVIS_RUNNER_TOKEN` (il runner raggiunge jarvis con un tunnel SSH su 127.0.0.1:8090).
"""

import functools
import hmac
import html
import json
import logging
import os
import re
import threading
from datetime import date as Date
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from jarvis import briefing, checks, commands, config, db, i18n, outcomes, planner, projects, runs
from jarvis.github import GitHub, GitHubError
from jarvis.i18n import t, words
from jarvis.ingest import IngestError, ingest
from jarvis.telegram import Telegram

MAX_BODY = 5 * 1024 * 1024

log = logging.getLogger("jarvis")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def valid_date(value: str) -> str:
    return Date.fromisoformat(value).isoformat()


class Handler(BaseHTTPRequestHandler):
    app: "App"

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            return self._send(200, {"ok": True})
        if not self._authorized():
            return
        if url.path == "/sources":
            return self._send(200, self.app.cfg.sources())
        if url.path == "/briefing":
            try:
                date = valid_date(parse_qs(url.query).get("date", [self.app.today()])[0])
            except ValueError:
                return self._send(400, {"error": t("api.bad_date")})
            with self.app.lock:
                result = planner.briefing(self.app.conn, date)
            if result is None:
                return self._send(404, {"error": t("api.no_briefing", date=date)})
            return self._send(200, {**result, "text": briefing.render(result)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        if self.path not in ("/ingest", "/plan", "/briefing/sent", "/reply", "/runner/claim", "/runner/update"):
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, {"error": t("api.too_large")})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as e:
            return self._send(400, {"error": str(e)})
        if not isinstance(payload, dict):
            return self._send(400, {"error": t("api.expected_object")})
        if self.path == "/ingest":
            return self._ingest(payload)
        if self.path == "/briefing/sent":
            return self._sent(payload)
        if self.path == "/reply":
            try:
                return self._reply(payload)
            except Exception as e:  # noqa: BLE001 - un errore interno deve arrivare su Telegram, non sparire
                log.exception("errore interno su /reply")
                if getattr(self, "_headers_sent", False):
                    return
                text = t("server.internal_error",
                         error=html.escape(f"{type(e).__name__}: {e}"[:300], quote=False))
                return self._send(200, {"message": self.app.message(text, int(payload.get("message_id") or 0) or None),
                                        "is_briefing": False})
        if self.path == "/runner/claim":
            return self._claim()
        if self.path == "/runner/update":
            return self._run_update(payload)
        self._plan(payload)

    def _claim(self):
        cfg = self.app.cfg
        with self.app.lock:
            checks.record(self.app.conn, "runner_seen", utc_now())
            stale = runs.expire_stale(self.app.conn, cfg.runner["stale_hours"])
            job = runs.claim(self.app.conn, cfg)
        for run_id in stale:
            self.app.notify_run(run_id)
        if job:
            log.info("run %s al runner: %s %s", job["run_id"], job["executor"]["name"], job["task"]["external_id"])
            self.app.notify_run(job["run_id"])
        self._send(200, {"run": job})

    def _run_update(self, payload: dict):
        try:
            run_id = int(payload["run_id"])
            with self.app.lock:
                checks.record(self.app.conn, "runner_seen", utc_now())
                run = runs.update(self.app.conn, run_id, payload, self.app.cfg)
        except (KeyError, TypeError, ValueError) as e:
            return self._send(400, {"error": str(e)})
        if run["status"] != "running":
            log.info("run %s: %s %s", run_id, run["status"], run["pr_url"] or run["error"])
            self.app.notify_run(run_id)
        cancel = run["status"] == "running" and bool(run["cancel_requested"])
        self._send(200, {"ok": True, "status": run["status"], "cancel": cancel})

    def _ingest(self, payload: dict):
        try:
            with self.app.lock:
                counts = ingest(self.app.conn, self.app.cfg, payload, utc_now())
        except IngestError as e:
            return self._send(400, {"error": str(e)})
        log.info("ingest %s", counts)
        self._send(200, counts)

    def _plan(self, payload: dict):
        try:
            date = valid_date(payload.get("date") or self.app.today())
            hours = payload.get("available_hours")
            hours = None if hours is None else float(hours)
        except (TypeError, ValueError):
            return self._send(400, {"error": t("api.bad_plan_args")})
        try:
            result = self.app.run_planner(date, hours, force=bool(payload.get("force")))
        except planner.BriefingLocked as e:
            if payload.get("if_missing"):
                return self._send(200, {"skipped": str(e), "message": None, "is_briefing": False})
            return self._send(409, {"error": str(e)})
        except planner.PlannerError as e:
            if payload.get("if_missing"):  # briefing automatico: l'errore arriva su Telegram
                return self._send(200, {"error": str(e), "message": self.app.message(_planner_failed(e)),
                                        "is_briefing": False})
            return self._send(502, {"error": str(e)})
        text = briefing.render(result, self.app.notes())
        self._send(200, {**result, "message": self.app.message(text), "is_briefing": True})

    def _sent(self, payload: dict):
        try:
            date, message_id = valid_date(payload["date"]), int(payload["message_id"])
        except (KeyError, TypeError, ValueError):
            return self._send(400, {"error": t("api.need_date_message")})
        with self.app.lock:
            ok = briefing.mark_sent(self.app.conn, date, message_id)
        return self._send(200, {"ok": True}) if ok else self._send(404, {"error": t("api.nothing_to_send", date=date)})

    def _reply(self, payload: dict):
        tg = self.app.telegram
        if not tg:
            return self._send(503, {"error": t("api.telegram_unconfigured")})
        ignored = {"message": None, "is_briefing": False}
        try:
            origin = (int(payload["chat_id"]), int(payload.get("thread_id") or 0), int(payload["from_id"]))
            message_id, text = int(payload["message_id"]), payload.get("text")
        except (KeyError, TypeError, ValueError):
            return self._send(400, {"error": t("api.need_chat")})
        # Solo l'owner, solo nel topic Jarvis: tutto il resto viene ignorato senza risposta.
        if origin != (tg["chat_id"], tg["thread_id"], tg["owner_id"]) or not isinstance(text, str) or not text.strip():
            log.info("reply ignorata da chat %s thread %s utente %s", *origin)
            if origin[0] == tg["chat_id"] and origin[2] == tg["owner_id"] and self._looks_like_jarvis(text):
                self.app.wrong_topic(origin[1], message_id)
            return self._send(200, {**ignored, "ignored": True})
        word = " ".join(text.strip().lower().split()).rstrip(".!")
        if word in words("help_words"):
            return self._send(200, {"message": self.app.message(self.app.full_help(), message_id),
                                    "is_briefing": False})
        if word in words("show_words"):
            return self._show_briefing(message_id)
        if word in words("projects_words"):
            with self.app.lock:
                current = planner.briefing(self.app.conn, self.app.today())
                text = projects.text(self.app.conn, self.app.cfg, current["focus"] if current else None)
            return self._send(200, {"message": self.app.message(text, message_id), "is_briefing": False})
        if word in words("outcome_words"):
            with self.app.lock:
                text = outcomes.stats_text(self.app.conn)
            return self._send(200, {"message": self.app.message(text, message_id), "is_briefing": False})
        if word in words("queue_words") or word in words("clear_words"):
            with self.app.lock:
                cleared = runs.clear(self.app.conn) if word in words("clear_words") else None
                text = runs.status_text(self.app.conn, checks.get(self.app.conn, "runner_seen"))
            if cleared is not None:
                text = t("server.cleared", count=cleared) + text
            return self._send(200, {"message": self.app.message(text, message_id), "is_briefing": False})
        if word in words("stop_follow"):
            return self._stop_follow(message_id)
        if m := runs.LOG.fullmatch(word):
            return self._log(message_id, word.startswith(tuple(words("follow_words"))),
                             int(m.group(1)) if m.group(1) else None)
        plan_cmd = self._plan_command(word, text.strip())
        if plan_cmd is not None:
            with self.app.lock:
                try:
                    reply = plan_cmd()
                    reply += "\n\n" + runs.status_text(self.app.conn, checks.get(self.app.conn, "runner_seen"))
                except runs.RunError as e:
                    reply = t("server.warn", error=html.escape(str(e), quote=False))
            return self._send(200, {"message": self.app.message(reply, message_id), "is_briefing": False})
        if r := runs.REPAIR.fullmatch(word):
            names = briefing.aliases(self.app.cfg)
            executor = None
            if r.group(2):
                executor = names.get(r.group(2).strip()) or names.get(r.group(2).strip().replace(" ", "-"))
            with self.app.lock:
                try:
                    if r.group(2) and executor is None:
                        raise runs.RunError(t("err.unknown_executor", name=r.group(2).strip()))
                    reply = runs.repair(self.app.conn, self.app.cfg, int(r.group(1)), executor)
                    reply += "\n\n" + runs.status_text(self.app.conn, checks.get(self.app.conn, "runner_seen"))
                except runs.RunError as e:
                    reply = t("server.warn", error=html.escape(str(e), quote=False))
            return self._send(200, {"message": self.app.message(reply, message_id), "is_briefing": False})
        if m := runs.STOP.fullmatch(word):
            with self.app.lock:
                try:
                    reply = runs.stop(self.app.conn, int(m.group(1)) if m.group(1) else None)
                except runs.RunError as e:
                    reply = t("server.warn", error=html.escape(str(e), quote=False))
            return self._send(200, {"message": self.app.message(reply, message_id), "is_briefing": False})
        go = runs.parse_go(text)
        text_no_base, base = runs.split_base(text)
        text_no_base, plan = runs.split_plan(text_no_base)
        word_no_base = " ".join(text_no_base.strip().lower().split()).rstrip(".!")
        if go is None and (m := runs.GO_REF.fullmatch(word_no_base)):
            try:
                add = briefing.added_task(m.group(1), briefing.aliases(self.app.cfg))
            except briefing.ReplyError as e:
                reply = t("server.warn_nothing_queued", error=html.escape(str(e), quote=False))
                return self._send(200, {"message": self.app.message(reply, message_id), "is_briefing": False})
            if add:
                go = {"positions": [], "executor": None, "add": add}
                if base:
                    go["base"] = base
                if plan:
                    go["plan"] = True
        if go is not None:
            return self._go(message_id, go)
        if self.app.github and (commands.is_yes(word) or commands.is_no(word)):
            return self._decide(message_id, text, commands.is_yes(word))
        # "sì crea issue": con un'azione in attesa conta la prima parola, il resto non va all'interprete.
        if self.app.github and (yes := commands.decision(word)) is not None:
            with self.app.lock:
                pending = commands.has_pending(self.app.conn, self.app.cfg.commands["confirm_minutes"])
            if pending:
                return self._decide(message_id, text, yes)
        try:
            parsed, error = briefing.parse(text, self.app.cfg), None
        except briefing.ReplyError as e:
            parsed, error = None, str(e)
            # Tutto ciò che non sembra un comando del briefing ("2 ...", "ore ...") è una richiesta GitHub.
            if self.app.github and not _briefing_like().match(word):
                return self._github_command(message_id, text)
        self._briefing_reply(message_id, text, parsed, error)

    def _plan_command(self, word: str, text: str):
        """"approva N" / "scarta N" / "modifica N <testo>" come funzione da eseguire col lock, o None se il messaggio
        non è un comando sul piano ("modifica ..." senza numero conta solo se c'è un piano in attesa)."""
        conn = self.app.conn
        if m := runs.APPROVE.fullmatch(word):
            return lambda: runs.approve_plan(conn, int(m.group(1)) if m.group(1) else None)
        if m := runs.DISCARD.fullmatch(word):
            return lambda: runs.discard_plan(conn, int(m.group(1)) if m.group(1) else None)
        if m := runs.REVISE.fullmatch(text):
            if m.group(1):
                return lambda: runs.revise_plan(conn, int(m.group(1)), m.group(2))
            with self.app.lock:
                waiting = runs.plans_waiting(conn)
            if waiting:
                return lambda: runs.revise_plan(conn, None, m.group(2))
        return None

    def _log(self, message_id: int, follow: bool, run_id: int | None):
        """`log`: istantanea delle ultime righe. `segui`: messaggio che Jarvis aggiorna da solo finché il run finisce."""
        with self.app.lock:
            try:
                run = runs.log_run(self.app.conn, run_id)
            except runs.RunError as e:
                text = t("server.warn", error=html.escape(str(e), quote=False))
                return self._send(200, {"message": self.app.message(text, message_id), "is_briefing": False})
            text = runs.log_text(self.app.conn, run, lines=15 if follow else 25)
            seen = self.app.log_cursor(run["id"])
        if not follow or run["status"] != "running":
            if follow:
                text += "\n" + t("server.not_following_idle")
            return self._send(200, {"message": self.app.message(text, message_id), "is_briefing": False})
        if not self.app.notifier:
            text = t("server.no_direct_messages")
            return self._send(200, {"message": self.app.message(text, message_id), "is_briefing": False})
        sent = self.app.notifier.send(text + "\n" + t("server.follow_footer"))
        if sent:
            self.app.following[run["id"]] = {"message_id": sent, "seen": seen, "status": run["status"]}
        self._send(200, {"message": None, "is_briefing": False})

    def _stop_follow(self, message_id: int):
        stopped = self.app.stop_follow()
        text = (t("server.stopped_following", runs=", ".join("#" + str(r) for r in stopped)) if stopped
                else t("server.not_following"))
        self._send(200, {"message": self.app.message(text, message_id), "is_briefing": False})

    def _looks_like_jarvis(self, text) -> bool:
        """Comando Jarvis scritto dall'owner in un altro topic: vale un avviso, il resto si ignora."""
        if not isinstance(text, str) or not text.strip():
            return False
        word = " ".join(text.strip().lower().split()).rstrip(".!")
        if any(word in words(key) for key in ("help_words", "show_words", "queue_words", "clear_words",
                                              "outcome_words", "projects_words")):
            return True
        if (runs.parse_go(text) is not None or runs.STOP.fullmatch(word) or runs.REPAIR.fullmatch(word)
                or runs.LOG.fullmatch(word) or word in words("stop_follow") or runs.APPROVE.fullmatch(word)
                or runs.DISCARD.fullmatch(word)):
            return True
        try:
            briefing.parse(text, self.app.cfg)
        except briefing.ReplyError:
            return False
        return True

    def _go(self, message_id: int, go: dict):
        executor = None
        if go["executor"]:
            names = briefing.aliases(self.app.cfg)
            executor = names.get(go["executor"]) or names.get(go["executor"].replace(" ", "-"))
            if executor is None:
                text = t("server.warn_nothing_queued",
                         error=t("err.unknown_executor", name=html.escape(go["executor"], quote=False)))
                return self._send(200, {"message": self.app.message(text, message_id), "is_briefing": False})
        with self.app.lock:
            current = planner.briefing(self.app.conn, self.app.today())
            if current is None or current["status"] in ("failed", "expired"):
                text = t("server.no_valid_briefing")
            else:
                try:
                    self.app.conn.execute("BEGIN")
                    positions = go["positions"]
                    if go.get("add"):  # "vai <ticket> <esecutore>": aggiunge al briefing e mette in coda
                        task = briefing.find_task(self.app.conn, go["add"]["source"], go["add"]["ref"])
                        if self.app.conn.execute(
                            "SELECT 1 FROM runs WHERE task_id = ? AND status IN ('queued', 'running')", (task["id"],)
                        ).fetchone():
                            raise runs.RunError(t("server.err.already_active", ref=briefing.short_ref(task["external_id"])))
                        added = briefing.apply(self.app.conn, current["id"], [go["add"]], self.app.cfg)
                        position = int(added[0].split()[0])
                        self.app.conn.execute(
                            "UPDATE briefing_items SET status = 'approved' WHERE briefing_id = ? AND position = ?",
                            (current["id"], position),
                        )
                        positions = [position]
                    done = runs.queue(self.app.conn, self.app.cfg, current["id"], positions, executor, go.get("base"),
                                      plan=go.get("plan", False))
                    self.app.conn.execute("COMMIT")
                except (runs.RunError, briefing.ReplyError) as e:
                    self.app.conn.execute("ROLLBACK")
                    text = t("server.warn_nothing_queued", error=html.escape(str(e), quote=False))
                else:
                    log.info("in coda: %s", done)
                    planning = any(t("runs.planning_first") in d for d in done)
                    text = (t("server.queued_plan" if planning else "server.queued",
                              done=html.escape(", ".join(done), quote=False))
                            + runs.status_text(self.app.conn, checks.get(self.app.conn, "runner_seen")))
        self._send(200, {"message": self.app.message(text, message_id), "is_briefing": False})

    def _show_briefing(self, message_id: int):
        with self.app.lock:
            current = planner.briefing(self.app.conn, self.app.today())
        if current is None or current["status"] == "failed":
            # Nessun briefing o planner fallito (es. quota di Claude finita stamattina): si rigenera adesso,
            # invece di rimostrare l'errore vecchio.
            hours = current["available_hours"] if current else None
            try:
                current = self.app.run_planner(self.app.today(), hours, force=True, focus=current["focus"] if current else None)
            except planner.PlannerError as e:
                return self._send(200, {"message": self.app.message(_planner_failed(e), message_id),
                                        "is_briefing": False})
            return self._send(200, {"message": self.app.message(briefing.render(current, self.app.notes())),
                                    "is_briefing": True, "date": self.app.today()})
        else:
            text = briefing.render(current, self.app.notes())
        self._send(200, {"message": self.app.message(text, message_id), "is_briefing": False})

    def _regenerate(self, message_id: int, text: str, commands: list):
        date = self.app.today()
        with self.app.lock:
            current = planner.briefing(self.app.conn, date)
            if current and not briefing.record_reply(self.app.conn, current["id"], message_id, text, commands):
                return self._send(200, {"message": None, "is_briefing": False, "duplicate": True})
        cmd = commands[0]
        # "ore 4" tiene ore e focus già in uso; "briefing su X" cambia il focus e tiene le ore; "su tutti" lo toglie
        hours = cmd["hours"] if cmd["hours"] is not None else (current["available_hours"] if current else None)
        focus = cmd["focus"] if cmd.get("set_focus") else (current["focus"] if current else None)
        try:
            result = self.app.run_planner(date, hours, force=True, focus=focus)
        except planner.PlannerError as e:
            return self._send(200, {"message": self.app.message(_planner_failed(e), message_id),
                                    "is_briefing": False})
        return self._send(200, {"message": self.app.message(briefing.render(result, self.app.notes())),
                                "is_briefing": True, "date": date})

    def _briefing_reply(self, message_id: int, text: str, commands: list | None, error: str | None):
        if error is None and commands[0]["cmd"] == "hours":  # funziona anche se il briefing è fallito
            return self._regenerate(message_id, text, commands)
        date = self.app.today()
        with self.app.lock:
            current = planner.briefing(self.app.conn, date)
            if current is None or current["status"] in ("failed", "expired"):
                hint = t("server.hint_planner_failed" if current and current["status"] == "failed"
                         else "server.hint_generate")
                return self._send(200, {"message": self.app.message(t("server.no_valid_briefing_hint", hint=hint),
                                                                    message_id), "is_briefing": False})
            if not briefing.record_reply(self.app.conn, current["id"], message_id, text, commands):
                return self._send(200, {"message": None, "is_briefing": False, "duplicate": True})
            if error is None:
                try:
                    self.app.conn.execute("BEGIN")
                    done = briefing.apply(self.app.conn, current["id"], commands, self.app.cfg)
                    self.app.conn.execute("COMMIT")
                except briefing.ReplyError as e:
                    self.app.conn.execute("ROLLBACK")
                    error = str(e)
                else:
                    updated = planner.briefing(self.app.conn, date)
        if error:
            reply = t("server.warn_no_change", error=html.escape(error, quote=False), help=self.app.help())
            return self._send(200, {"message": self.app.message(reply, message_id), "is_briefing": False})
        log.info("reply %s: %s", date, done)
        if updated["status"] == "confirmed":
            head = t("server.applied_confirmed", done=", ".join(done))
        else:
            head = t("server.applied_unconfirmed", done=", ".join(done))
        ack = head + "\n\n" + briefing.render(updated)
        self._send(200, {"message": self.app.message(ack, message_id), "is_briefing": False})

    def _github_command(self, message_id: int, text: str):
        cfg, duplicate = self.app.cfg, {"message": None, "is_briefing": False, "duplicate": True}
        with self.app.lock:
            if commands.seen(self.app.conn, message_id):
                return self._send(200, duplicate)
            previous = commands.recent_question(self.app.conn)
        action, error = None, None
        try:
            projects = commands.fetch_projects(cfg, self.app.github)
            with self.app.lock:
                tickets = commands.tickets(self.app.conn, text + " " + (previous or {}).get("request", ""))
            raw = commands.interpret(cfg, text, projects, self.app.commands_runner, previous, tickets)
            action = commands.validate(cfg, raw, projects)
        except commands.CommandError as e:
            error = str(e)
        except (GitHubError, planner.PlannerError) as e:
            log.error("comando GitHub: %s", e)
            error = t("server.err.generic", error=e)
        with self.app.lock:
            if not commands.save(self.app.conn, message_id, text, action, error):
                return self._send(200, duplicate)
        if action is None:
            reply = t("server.confused", error=html.escape(error, quote=False), help=self.app.help())
        else:
            log.info("comando GitHub in attesa: %s %s", action["action"], action["repo"])
            reply = commands.preview(action, cfg.commands["confirm_minutes"])
        self._send(200, {"message": self.app.message(reply, message_id), "is_briefing": False})

    def _decide(self, message_id: int, text: str, yes: bool):
        with self.app.lock:
            if commands.seen(self.app.conn, message_id):
                return self._send(200, {"message": None, "is_briefing": False, "duplicate": True})
            pending = commands.take_pending(self.app.conn, message_id, self.app.cfg.commands["confirm_minutes"],
                                            "executed" if yes else "cancelled")
            question = pending is None and commands.recent_question(self.app.conn)
        if question:  # "sì"/"no" in risposta a una domanda dell'interprete
            return self._github_command(message_id, text)
        if pending is None:
            reply = t("server.no_pending_action")
        elif not yes:
            reply = t("server.cancelled_action")
        else:
            try:
                result, error = commands.execute(self.app.github, pending), None
            except GitHubError as e:
                result, error = getattr(e, "partial", {}), str(e)
            task = None if error else commands.imported_task(self.app.cfg, pending, result)
            with self.app.lock:
                commands.finish(self.app.conn, pending["id"], result, error)
                if task:
                    ingest(self.app.conn, self.app.cfg, {"tasks": [task]}, utc_now())
            log.info("comando GitHub %s: %s %s", pending["id"], "fallito" if error else "eseguito", result)
            reply = _result_text(pending, result, error)
            if task and self.app.runner_token:
                ref = html.escape(briefing.short_ref(task["external_id"]), quote=False)
                reply += "\n" + t("server.work_hint", ref=ref)
        self._send(200, {"message": self.app.message(reply, message_id), "is_briefing": False})

    def _authorized(self) -> bool:
        token = self.app.runner_token if self.path.startswith("/runner/") else self.app.token
        if not token:
            self._send(503, {"error": t("api.runner_token_missing")})
            return False
        expected = f"Bearer {token}"
        if hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            return True
        self._send(401, {"error": "unauthorized"})
        return False

    def _send(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self._headers_sent = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)


@functools.lru_cache(maxsize=None)
def _briefing_like_for(locale: str) -> re.Pattern:
    return re.compile(rf"(\d+|{i18n.alternation('hours_word')}|{i18n.alternation('briefing_word')})(\s|$)")


def _briefing_like() -> re.Pattern:
    """Un messaggio che comincia con un numero, "ore" o "briefing" è un comando del briefing (anche se sbagliato)."""
    return _briefing_like_for(i18n.get_locale())


def _planner_failed(error) -> str:
    text = t("server.planner_failed", error=html.escape(str(error)[:500], quote=False))
    hint = checks.planner_hint(str(error))
    return text + "\n\n" + (hint or t("server.planner_retry"))


def _result_text(action: dict, result: dict, error: str | None) -> str:
    e = lambda s: html.escape(str(s), quote=False)  # noqa: E731
    lines = []
    if result.get("url"):
        ref = f"{action['repo']}#{result.get('number') or action['issue_number']}"
        lines.append(f'<a href="{html.escape(result["url"], quote=True)}">{e(ref)}</a>')
    if result.get("assigned"):
        lines.append(t("result.assigned", names=e(", ".join(result["assigned"]))))
    if result.get("added_to"):
        lines.append(t("result.added_to", board=e(result["added_to"])))
    if result.get("status"):
        lines.append(t("result.status", status=e(result["status"])))
    done = " · ".join(lines)
    if error:
        return t("result.github_error", error=e(error)) + (t("result.already_done", done=done) if done else "")
    return t("result.done", done=done)


class _Result:
    """Risultato letto per intero mentre il lock è preso."""

    def __init__(self, cursor):
        self.rows = cursor.fetchall()
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class LockedConnection:
    """Connessione condivisa che prende il lock del DB a ogni execute.

    Una transazione BEGIN...COMMIT tiene il lock dall'inizio alla fine, così le
    scritture del planner non si intrecciano con un ingest concorrente.
    """

    def __init__(self, app: "App"):
        self.app = app
        self.in_tx = False

    def execute(self, sql: str, params=()):
        statement = sql.strip().upper()
        if not self.in_tx:
            self.app.lock.acquire()
        try:
            return _Result(self.app.conn.execute(sql, params))
        finally:
            if statement == "BEGIN":
                self.in_tx = True
            elif statement in ("COMMIT", "ROLLBACK"):
                self.in_tx = False
            if not self.in_tx:
                self.app.lock.release()


class App:
    def __init__(self, cfg: config.Config, db_path: str, token: str, runner=None,
                 telegram: dict | None = None, github: GitHub | None = None, runner_token: str = "",
                 notifier: Telegram | None = None):
        if not token:
            raise ValueError("JARVIS_API_TOKEN mancante")
        self.cfg = cfg
        i18n.set_locale(cfg.locale)
        self.conn = db.connect(db_path)
        self.token = token
        # runner: stub dei test per planner e comandi; altrimenti il provider di ogni esecutore configurato
        self.runner = runner or planner.runner_for(cfg, "planner")
        self.commands_runner = runner or (planner.runner_for(cfg, "commands") if cfg.commands else None)
        self.lock = threading.Lock()  # una sola connessione SQLite condivisa
        self.plan_lock = threading.Lock()  # un solo planner alla volta
        self.telegram = telegram  # {chat_id, thread_id, owner_id}; None: /reply disabilitato
        self.github = github if cfg.commands else None  # None: comandi GitHub disabilitati
        self.runner_token = runner_token  # vuoto: rotte /runner/ disabilitate
        self.notifier = notifier  # None: gli eventi del runner restano solo nel DB ("coda")
        self.following: dict[int, dict] = {}  # run_id -> {message_id, seen (ultimo id di log), status}: "segui"

    def wrong_topic(self, thread_id: int, message_id: int) -> None:
        if self.notifier:
            self.notifier.send(t("server.wrong_topic"), thread_id=thread_id or None, reply_to=message_id)

    def check_outcomes(self) -> None:
        """Esiti delle PR e riallineamenti del submodule (fase 3): gira ogni outcomes.check_minutes."""
        with self.lock:
            runs.prune_logs(self.conn)
        if not self.github:
            return
        with self.lock:
            messages = outcomes.refresh(self.conn, self.github)
        for text in messages:
            if self.notifier:
                self.notifier.send(text)

    def log_cursor(self, run_id: int) -> int:
        row = self.conn.execute("SELECT MAX(id) FROM run_log WHERE run_id = ?", (run_id,)).fetchone()
        return row[0] or 0

    def stop_follow(self) -> list[int]:
        stopped = list(self.following)
        for run_id, follow in list(self.following.items()):
            if self.notifier:
                self.notifier.edit(follow["message_id"], t("server.follow_stopped", run_id=run_id))
        self.following.clear()
        return stopped

    def follow_tick(self) -> None:
        """Aggiorna i messaggi di "segui" quando ci sono righe nuove o il run è cambiato; li chiude a fine run."""
        if not self.notifier:
            return
        for run_id, follow in list(self.following.items()):
            with self.lock:
                run = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
                last = self.log_cursor(run_id)
                running = run["status"] == "running"
                if running and last == follow["seen"]:
                    continue
                text = runs.log_text(self.conn, run, lines=15)
            if not running:
                text += "\n" + t("server.follow_finished")
                self.following.pop(run_id, None)
            else:
                text += "\n" + t("server.follow_hint")
            follow["seen"] = last
            self.notifier.edit(follow["message_id"], text)

    def watch_follow(self, seconds: float = 10) -> None:
        while True:
            try:
                self.follow_tick()
            except Exception:  # noqa: BLE001 - riprova al giro dopo
                log.exception("aggiornamento di segui fallito")
            threading.Event().wait(seconds)

    def watch_outcomes(self) -> None:
        interval = self.cfg.outcomes["check_minutes"] * 60
        while True:
            try:
                self.check_outcomes()
            except Exception:  # noqa: BLE001 - il controllo riprova al giro dopo
                log.exception("controllo esiti fallito")
            threading.Event().wait(interval)

    def notify_run(self, run_id: int) -> None:
        with self.lock:
            run = dict(self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())
            text = runs.notification(self.conn, run) if self.notifier else None
        if text:
            self.notifier.send(text)

    def help(self) -> str:
        text = t("server.help_briefing", commands=t("briefing.help_commands"))
        if self.github:
            text += "\n" + t("server.help_github")
        return text

    def full_help(self) -> str:
        executors = ", ".join(f"<code>{n}</code>" for n in ("sonnet", "opus", "haiku", "agy", words("human")[0]))
        text = t("help.full", executors=executors)
        if self.github:
            text += "\n\n" + t("help.github", owner=self.cfg.owner_github)
        return text

    def today(self) -> str:
        return datetime.now(self.cfg.tz).date().isoformat()

    def notes(self) -> list[str]:
        """Avvisi per il briefing (testo HTML). Non devono mai impedire l'invio."""
        try:
            now = datetime.now(self.cfg.tz)
            with self.lock:
                found = checks.stale_ingest(self.conn, now, self.cfg)
            found += checks.github_token(self.github, now.date())
        except Exception as e:  # noqa: BLE001
            log.error("controlli del briefing: %s", e)
            return [t("server.checks_failed", error=html.escape(str(e), quote=False))]
        return [html.escape(n, quote=False) for n in found]

    def run_planner(self, date: str, hours: float | None, force: bool, focus: list[str] | None = None) -> dict:
        if not self.plan_lock.acquire(blocking=False):
            raise planner.BriefingLocked(t("planner.err.busy"))
        try:
            # La chiamata a claude dura decine di secondi: il lock del DB resta libero per l'ingest.
            planner.plan(LockedConnection(self), self.cfg, date, hours, force=force, runner=self.runner, focus=focus)
        except planner.PlannerError as e:
            if not isinstance(e, planner.BriefingLocked):
                log.error("planner: %s", e)
            raise
        finally:
            self.plan_lock.release()
        with self.lock:
            result = planner.briefing(self.conn, date)
        log.info("plan %s: %d voci", date, len(result["items"]))
        return result

    def message(self, text: str, reply_to: int | None = None) -> dict:
        tg = self.telegram or {}
        return {"chat_id": tg.get("chat_id"), "thread_id": tg.get("thread_id"), "text": text,
                "reply_to_message_id": reply_to}

    def server(self, host: str, port: int) -> ThreadingHTTPServer:
        handler = type("BoundHandler", (Handler,), {"app": self})
        return ThreadingHTTPServer((host, port), handler)


def telegram_from_env() -> dict | None:
    keys = {"chat_id": "TELEGRAM_JARVIS_CHAT_ID", "thread_id": "TELEGRAM_JARVIS_THREAD_ID",
            "owner_id": "TELEGRAM_JARVIS_OWNER_ID"}
    values = {k: os.environ.get(v, "").strip() for k, v in keys.items()}
    if not all(values.values()):
        log.warning("Telegram non configurato: /reply disabilitato")
        return None
    return {k: int(v) for k, v in values.items()}


def notifier_from_env() -> Telegram | None:
    tg, token = telegram_from_env(), os.environ.get("TELEGRAM_JARVIS_BOT_TOKEN", "").strip()
    if not tg or not token:
        log.warning("TELEGRAM_JARVIS_BOT_TOKEN mancante: eventi del runner non notificati")
        return None
    return Telegram(token, tg["chat_id"], tg["thread_id"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = App(
        cfg=config.load(os.environ.get("JARVIS_CONFIG", "config/projects.yaml")),
        db_path=os.environ.get("JARVIS_DB", "jarvis.db"),
        token=os.environ.get("JARVIS_API_TOKEN", ""),
        telegram=telegram_from_env(),
        github=GitHub(token) if (token := os.environ.get("GITHUB_JARVIS_TOKEN", "").strip()) else None,
        runner_token=os.environ.get("JARVIS_RUNNER_TOKEN", "").strip(),
        notifier=notifier_from_env(),
    )
    threading.Thread(target=app.watch_outcomes, name="outcomes", daemon=True).start()
    threading.Thread(target=app.watch_follow, name="follow", daemon=True).start()
    port = int(os.environ.get("JARVIS_PORT", "8080"))
    log.info("jarvis in ascolto su :%d", port)
    app.server("0.0.0.0", port).serve_forever()


if __name__ == "__main__":
    main()
