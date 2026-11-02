"""Coda di esecuzione (fase 2): "vai N" da Telegram, claim e aggiornamenti dal runner sul Mac.

Il runner non è mai raggiungibile da fuori: è lui a chiedere lavoro (POST /runner/claim) e a
riportare i progressi (POST /runner/update) attraverso un tunnel SSH verso il VPS.
"""

import functools
import html
import json
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from jarvis import plantext, providers
from jarvis.briefing import short_ref
from jarvis.config import Config
from jarvis.i18n import alternation, get_locale, messages, t, words
from jarvis.outcomes import feedback_line

log = logging.getLogger(__name__)

BASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
LOG_KEEP_DAYS = 7
LOG_PER_RUN = 300
RUNNER_OFFLINE_MINUTES = 3  # il runner chiede lavoro ogni minuto
PATTERN_NAMES = ("GO", "GO_REF", "BASE", "NOTE", "PLAN_WORD", "TOGETHER_WORD", "APPROVE", "DISCARD", "REVISE", "LOG", "REPAIR", "STOP")


@functools.lru_cache(maxsize=None)
def _patterns(locale: str) -> dict:
    """Regex dei comandi sui run per un locale: le parole (vai, approva, piano, ...) vengono da vocab.yaml."""
    the, plan, run = (alternation(k) for k in ("the_word", "plan_word", "run_word"))
    ref = rf"(?:\s+(?:(?:{the})\s+)?(?:(?:{plan})\s+)?(?:(?:{run})\s+)?#?(\d+))?"
    executor = r"(?:\s+(?:(?:" + alternation("with_word") + r")\s+)?([a-z][\w -]*))?"
    go = alternation("go_verbs")
    return {
        "GO": re.compile(rf"(?:{go})\s+({alternation('all_words')}|\d+(?:(?:\s*,\s*|\s+(?:{alternation('and_word')})\s+|\s+)\d+)*)"
                         + executor),
        "GO_REF": re.compile(rf"(?:{go})\s+(.+)"),  # "vai my-lib#12 sonnet"
        "BASE": re.compile(rf"(?:^|\s)(?:{alternation('base_word')})\s*[:=]\s*(\S+)", re.IGNORECASE),  # "vai 4 agy base:redesign"
        "NOTE": re.compile(rf"(?:^|\s)(?:{alternation('note_word')})\s*[:=]\s*(.+)$", re.IGNORECASE | re.DOTALL),  # "vai 4 nota: escludi X"
        "PLAN_WORD": re.compile(rf"(?:^|\s)(?:{plan})(?=\s|$)", re.IGNORECASE),  # "vai 4 piano agy"
        "TOGETHER_WORD": re.compile(rf"(?:^|\s)(?:{alternation('together_word')})(?=\s|$)", re.IGNORECASE),
        "APPROVE": re.compile(rf"(?:{alternation('approve_verbs')}){ref}"),
        "DISCARD": re.compile(rf"(?:{alternation('discard_verbs')}){ref}"),
        "REVISE": re.compile(rf"(?:{alternation('revise_verbs')})\s+(?:(?:(?:{the})\s+)?(?:(?:{plan})\s+)?(?:(?:{run})\s+)?#?(\d+)\s+)?(.+)",
                             re.IGNORECASE | re.DOTALL),
        "LOG": re.compile(rf"(?:{alternation('log_words')}|{alternation('follow_words')})(?:\s+(?:(?:{run})\s+)?#?(\d+))?"),
        "REPAIR": re.compile(rf"(?:{alternation('repair_verbs')})\s+(?:(?:{run})\s+)?#?(\d+)" + executor),
        "STOP": re.compile(rf"(?:{alternation('stop_verbs')})(?:\s+(?:(?:{run})\s+)?#?(\d+))?"),
    }


def __getattr__(name: str):
    """`runs.GO`, `runs.APPROVE`, ...: le regex del locale corrente."""
    if name in PATTERN_NAMES:
        return _patterns(get_locale())[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _rx(name: str) -> re.Pattern:
    return _patterns(get_locale())[name]


ACTIVE = ("queued", "running")
FINAL = ("pr_open", "done", "failed", "cancelled")
QUOTA_AVOID_HOURS = 1  # rate limit senza orario di reset: per quanto evitare quel provider
STEPS = ("worktree", "setup", "agent", "tests", "review", "fix", "push", "pr")


class RunError(ValueError):
    """Richiesta non eseguibile: il messaggio va mostrato all'owner."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def split_note(text: str) -> tuple[str, str | None]:
    """"vai 2 nota: escludi i rimborsi" -> ("vai 2", "escludi i rimborsi")."""
    m = _rx("NOTE").search(text)
    if m is None:
        return text, None
    return text[:m.start()].strip(), m.group(1).strip()


def split_base(text: str) -> tuple[str, str | None]:
    """"vai 4 agy base:Redesign" -> ("vai 4 agy", "Redesign"): il nome del branch resta com'è scritto (maiuscole)."""
    m = _rx("BASE").search(text)
    if m is None:
        return text, None
    return (text[:m.start()] + text[m.end():]).strip(), m.group(1)


def split_plan(text: str) -> tuple[str, bool]:
    """"vai 4 piano agy" -> ("vai 4 agy", True): la parola "piano" da sola chiede prima il piano."""
    stripped = re.sub(r"\s{2,}", " ", _rx("PLAN_WORD").sub(" ", text)).strip()
    return (stripped, True) if stripped != text.strip() else (text, False)


def split_together(text: str) -> tuple[str, bool]:
    """"vai 1 2 insieme" -> ("vai 1 2", True): l'owner li vuole in parallelo anche se il progetto o il planner
    direbbero di aspettare."""
    stripped = re.sub(r"\s{2,}", " ", _rx("TOGETHER_WORD").sub(" ", text)).strip()
    return (stripped, True) if stripped != text.strip() else (text, False)


def valid_base(name: str) -> bool:
    """Nome di branch ammesso come base: niente opzioni git, `..` o suffissi riservati."""
    return bool(BASE_NAME.fullmatch(name)) and ".." not in name and not name.endswith(("/", ".lock"))


def parse_go(text: str) -> dict | None:
    """"vai 1 3" -> {"positions": [1, 3], "executor": None}; "vai tutti" -> positions []; "vai 4 agy" ->
    executor "agy" (nome scritto dall'owner, da risolvere con gli alias). "vai 4 base:redesign" aggiunge
    "base" (override del branch di base, solo se indicato). "vai 4 nota:..." aggiunge "note". None se non è un comando di avvio."""
    text, note = split_note(text)
    text, base = split_base(text)
    text, plan = split_plan(text)
    text, together = split_together(text)
    m = _rx("GO").fullmatch(" ".join(text.strip().lower().split()).rstrip(".!"))
    if m is None:
        return None
    positions = [] if m.group(1) in words("all_words") else sorted({int(n) for n in re.findall(r"\d+", m.group(1))})
    go = {"positions": positions, "executor": m.group(2).strip() if m.group(2) else None}
    if base:
        go["base"] = base
    if plan:
        go["plan"] = True
    if together:
        go["together"] = True
    if note:
        go["note"] = note
    return go


def queue(conn: sqlite3.Connection, cfg: Config, briefing_id: int, positions: list[int],
          executor: str | None = None, base_branch: str | None = None, plan: bool = False,
          note: str | None = None, together: bool = False) -> tuple[list[str], list[tuple[str, int, int]]]:
    """Mette in coda le voci approvate (tutte se positions è vuota), con `executor` e `base_branch` se indicati.
    Con `plan` (o `plan_first` nel progetto) parte prima un run di piano: l'agente non tocca niente, il piano va
    all'owner e il lavoro parte solo con "approva N". `note` aggiunge eventuali istruzioni dell'owner.
    Restituisce le righe di riepilogo e l'elenco di (repo, issue_number, task_id) da assegnare a owner_github."""
    if base_branch and not valid_base(base_branch):
        raise RunError(t("runs.err.bad_base", base=base_branch))
    items = {
        r["position"]: r
        for r in conn.execute(
            """SELECT i.*, t.external_id, t.project, t.source, t.assignee FROM briefing_items i JOIN tasks t ON t.id = i.task_id
               WHERE i.briefing_id = ?""",
            (briefing_id,),
        ).fetchall()
    }
    executors = cfg.raw["executors"]
    if positions:
        missing = [p for p in positions if p not in items]
        if missing:
            raise RunError(t("err.no_item", position=", ".join(map(str, missing))))
        chosen = [items[p] for p in positions]
    else:
        chosen = [i for i in items.values() if i["status"] == "approved"
                  and not executors.get(i["final_executor"], {}).get("human")]
        if not chosen:
            raise RunError(t("runs.err.none_approved"))

    if executor:  # "vai 4 agy": cambia l'esecutore come "4 agy", poi mette in coda
        chosen = [{**dict(i), "final_executor": executor} for i in chosen]
    for i in chosen:
        name = i["final_executor"] or i["proposed_executor"]
        where = t("runs.item", position=i["position"])
        if i["status"] in ("queued", "done"):
            raise RunError(t("runs.err.already_queued" if i["status"] == "queued" else "runs.err.already_done",
                             where=where))
        if i["status"] != "approved":
            raise RunError(t("runs.err.not_approved", where=where, position=i["position"]))
        if executors.get(name, {}).get("human"):
            raise RunError(t("runs.err.human", where=where, position=i["position"]))
        if not providers.can(executors.get(name, {}).get("cli"), "execute"):
            raise RunError(t("runs.err.not_executable", where=where, name=name))
        if i["project"] is None or cfg.projects.get(i["project"], {}).get("paused"):
            raise RunError(t("runs.err.project_unavailable", where=where))

    now, done, to_assign = _iso(_now()), [], []
    group = f"g{briefing_id}-{secrets.token_hex(3)}" if together and len(chosen) > 1 else None
    for i in chosen:
        name = i["final_executor"] or i["proposed_executor"]
        planning = plan or bool(cfg.projects.get(i["project"], {}).get("plan_first"))
        conn.execute(
            """INSERT INTO runs (task_id, briefing_item_id, executor, model, status, queued_at, base_branch, kind,
                                 owner_note, parallel_group)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
            (i["task_id"], i["id"], name, executors[name]["model"], now, base_branch, "plan" if planning else "agent",
             note[:2000] if note else None, group),
        )
        conn.execute("UPDATE tasks SET skip_count = 0 WHERE id = ?", (i["task_id"],))  # ci si lavora: salti azzerati
        conn.execute(
            """UPDATE briefing_items SET status = 'queued', final_executor = ?,
                   decision_source = CASE WHEN ? THEN 'telegram' ELSE decision_source END WHERE id = ?""",
            (name, executor is not None, i["id"]),
        )
        row = dict(i)
        if row.get("source") == "github" and not row.get("assignee") and cfg.owner_github:
            external_id = row.get("external_id") or ""
            repo, _, num_str = external_id.partition("#")
            if repo in cfg.writable_repos() and num_str.isdigit():
                to_assign.append((repo, int(num_str), row["task_id"]))
        done.append(f"{i['position']} {short_ref(i['external_id'])} → {name}"
                    + (t("runs.planning_first") if planning else "")
                    + (t("runs.base_suffix", base=html.escape(base_branch, quote=False)) if base_branch else ""))
    return done, to_assign


def _only_running(conn: sqlite3.Connection) -> int | None:
    """Il run in corso quando l'owner non dice il numero: None se non ce n'è nessuno; con più run in corso il numero
    serve, e l'errore elenca quelli attivi."""
    rows = conn.execute("""SELECT r.id, t.external_id FROM runs r JOIN tasks t ON t.id = r.task_id
                           WHERE r.status = 'running' ORDER BY r.id""").fetchall()
    if len(rows) > 1:
        raise RunError(t("runs.err.which_run", n=len(rows),
                         runs=", ".join(f"#{r['id']} {html.escape(short_ref(r['external_id']))}" for r in rows)))
    return rows[0]["id"] if rows else None


def stop(conn: sqlite3.Connection, run_id: int | None) -> str:
    """"ferma N": un run in coda viene annullato subito, uno in corso al prossimo controllo del runner."""
    if run_id is None:
        run_id = _only_running(conn)
        if run_id is None:
            raise RunError(t("runs.err.none_running"))
    run = conn.execute("SELECT id, status, briefing_item_id FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise RunError(t("runs.err.no_run", run_id=run_id))
    if run["status"] == "queued":
        conn.execute("UPDATE runs SET status = 'cancelled', finished_at = ? WHERE id = ?", (_iso(_now()), run_id))
        conn.execute("UPDATE briefing_items SET status = 'approved' WHERE id = ?", (run["briefing_item_id"],))
        return t("runs.stop_queued", run_id=run_id)
    if run["status"] != "running":
        raise RunError(t("runs.err.not_running", run_id=run_id, status=run["status"]))
    conn.execute("UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,))
    return t("runs.stop_running", run_id=run_id)


def _open_plan(conn: sqlite3.Connection, run_id: int | None) -> sqlite3.Row:
    """Il piano in attesa: quello indicato, o l'unico aperto se l'owner non scrive il numero."""
    if run_id is None:
        rows = conn.execute("SELECT * FROM runs WHERE kind = 'plan' AND plan_state = 'proposed' ORDER BY id").fetchall()
        if not rows:
            raise RunError(t("runs.err.no_plan_waiting"))
        if len(rows) > 1:
            raise RunError(t("runs.err.many_plans", ids=", ".join(f"#{r['id']}" for r in rows)))
        return rows[0]
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None or run["kind"] != "plan":
        raise RunError(t("runs.err.not_a_plan", run_id=run_id))
    if run["plan_state"] != "proposed":
        state = t("runs.plan_state." + (run["plan_state"] if run["plan_state"] in ("approved", "revised", "discarded")
                                        else "not_ready"))
        raise RunError(t("runs.err.plan_state", run_id=run_id, state=state))
    return run


def approve_plan(conn: sqlite3.Connection, run_id: int | None) -> str:
    """"approva N": parte il lavoro vero, con il piano nel prompt (run agent con plan_of = piano)."""
    plan = _open_plan(conn, run_id)
    conn.execute("UPDATE runs SET plan_state = 'approved' WHERE id = ?", (plan["id"],))
    cur = conn.execute(
        """INSERT INTO runs (task_id, briefing_item_id, executor, model, status, queued_at, base_branch, kind,
                             plan_of, owner_note, parallel_group, depends_on_run)
           VALUES (?, ?, ?, ?, 'queued', ?, ?, 'agent', ?, ?, ?, ?)""",
        (plan["task_id"], plan["briefing_item_id"], plan["executor"], plan["model"], _iso(_now()),
         plan["base_branch"], plan["id"], plan["owner_note"], plan["parallel_group"], plan["depends_on_run"]),
    )
    return t("runs.plan_approved", plan_id=plan["id"], executor=html.escape(plan["executor"]), run_id=cur.lastrowid)


def revise_plan(conn: sqlite3.Connection, run_id: int | None, note: str) -> str:
    """"modifica N <testo>": nuovo run di piano che parte dal precedente e dalla nota dell'owner."""
    note = note.strip()
    if not note:
        raise RunError(t("runs.err.revise_note"))
    plan = _open_plan(conn, run_id)
    conn.execute("UPDATE runs SET plan_state = 'revised' WHERE id = ?", (plan["id"],))
    cur = conn.execute(
        """INSERT INTO runs (task_id, briefing_item_id, executor, model, status, queued_at, base_branch, kind,
                             plan_of, plan_note, owner_note, parallel_group, depends_on_run)
           VALUES (?, ?, ?, ?, 'queued', ?, ?, 'plan', ?, ?, ?, ?, ?)""",
        (plan["task_id"], plan["briefing_item_id"], plan["executor"], plan["model"], _iso(_now()),
         plan["base_branch"], plan["id"], note[:2000], plan["owner_note"], plan["parallel_group"],
         plan["depends_on_run"]),
    )
    return t("runs.plan_revised", run_id=cur.lastrowid, plan_id=plan["id"])


def discard_plan(conn: sqlite3.Connection, run_id: int | None) -> str:
    plan = _open_plan(conn, run_id)
    conn.execute("UPDATE runs SET plan_state = 'discarded' WHERE id = ?", (plan["id"],))
    conn.execute("UPDATE briefing_items SET status = 'approved' WHERE id = ?", (plan["briefing_item_id"],))
    return t("runs.plan_discarded", plan_id=plan["id"])


def plans_waiting(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""SELECT r.id, r.executor, t.external_id, t.url FROM runs r JOIN tasks t ON t.id = r.task_id
                           WHERE r.kind = 'plan' AND r.plan_state = 'proposed' ORDER BY r.id""").fetchall()


def add_log(conn: sqlite3.Connection, run_id: int, lines: list) -> None:
    """Righe di log dal runner (azioni dell'agente). Max LOG_PER_RUN per run: le più vecchie si perdono."""
    now = _iso(_now())
    clean = [str(line).replace("\n", " ")[:300] for line in lines[:100] if str(line).strip()]
    conn.executemany("INSERT INTO run_log (run_id, at, line) VALUES (?, ?, ?)", [(run_id, now, line) for line in clean])
    conn.execute("""DELETE FROM run_log WHERE run_id = ? AND id <= (
                        SELECT id FROM run_log WHERE run_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?)""",
                 (run_id, run_id, LOG_PER_RUN))


def prune_logs(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM run_log WHERE at < ?", (_iso(_now() - timedelta(days=LOG_KEEP_DAYS)),))


def log_run(conn: sqlite3.Connection, run_id: int | None) -> sqlite3.Row:
    """Il run indicato, o quello in corso, o l'ultimo che ha scritto log."""
    if run_id is None:
        running = _only_running(conn)
        run = (conn.execute("SELECT * FROM runs WHERE id = ?", (running,)).fetchone() if running else None) or (
               conn.execute("""SELECT * FROM runs WHERE id = (SELECT MAX(run_id) FROM run_log)""").fetchone())
        if run is None:
            raise RunError(t("runs.err.no_log"))
        return run
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise RunError(t("runs.err.no_run", run_id=run_id))
    return run


def _ago(iso: str | None) -> str:
    if not iso:
        return "-"
    secs = int((_now() - datetime.fromisoformat(iso)).total_seconds())
    return t("runs.ago_s", value=secs) if secs < 90 else t("runs.ago_min", value=secs // 60)


def log_text(conn: sqlite3.Connection, run: sqlite3.Row, lines: int = 25, max_chars: int = 3500) -> str:
    """Ultime righe del run, con passo e da quanto gira, per `log` e `segui`."""
    task = conn.execute("SELECT external_id FROM tasks WHERE id = ?", (run["task_id"],)).fetchone()
    icon = {"queued": "⏳", "running": "▶️", "pr_open": "✅", "done": "🏁", "failed": "❌", "cancelled": "⏹"}
    head = (t("runs.log_head", icon=icon.get(run["status"], "?"), run_id=run["id"],
              ref=html.escape(short_ref(task["external_id"])), executor=html.escape(run["executor"]),
              status=run["status"])
            + (f" · {html.escape(run['step'])}" if run["step"] and run["status"] == "running" else "")
            + (t("runs.since", ago=_ago(run["started_at"])) if run["status"] == "running" else ""))
    rows = conn.execute("SELECT line FROM run_log WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                        (run["id"], lines)).fetchall()
    body = [html.escape(r["line"], quote=False) for r in reversed(rows)]
    if not body:
        return head + "\n" + t("runs.log_empty")
    text = head + "\n" + "\n".join(body)
    while len(text) > max_chars and len(body) > 1:  # tiene le righe più recenti
        body.pop(0)
        text = head + "\n…\n" + "\n".join(body)
    return text


def repair(conn: sqlite3.Connection, cfg: Config, run_id: int, executor: str | None) -> str:
    """"ripara N": riprende le PR aperte del run N (stessi branch), rilancia test, correzione e review."""
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise RunError(t("runs.err.no_run", run_id=run_id))
    if run["kind"] == "repair":  # "ripara" su una riparazione: si riparte dal run originale
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run["parent_run_id"],)).fetchone()
    if run["kind"] != "agent" or run["status"] != "pr_open" or not run["branch"]:
        raise RunError(t("runs.err.no_open_prs", run_id=run["id"]))
    if conn.execute("SELECT 1 FROM runs WHERE parent_run_id = ? AND kind IN ('repair', 'realign') "
                    "AND status IN ('queued', 'running')", (run["id"],)).fetchone():
        raise RunError(t("runs.err.repair_queued", run_id=run["id"]))
    name = executor or run["executor"]
    if not providers.can(cfg.raw["executors"].get(name, {}).get("cli"), "execute"):
        raise RunError(t("runs.err.executor_not_executable", name=name))
    # PR impilata su una PR ora mergiata: la riparazione la ribasa sulla base normale (rebase --onto) e la ripunta lì.
    stacked = conn.execute("SELECT * FROM runs WHERE id = ?", (run["depends_on_run"],)).fetchone() \
        if run["depends_on_run"] else None
    rebase = bool(stacked and run["base_branch"] == stacked["branch"] and stacked["status"] == "done"
                  and stacked["pr_outcome"] in APPROVED)
    base = (cfg.projects.get(run["project"] or "", {}).get("base_branch") if rebase else run["base_branch"])
    # Commenti e CI rossa visti da outcomes.refresh: passano all'agente insieme ai test falliti.
    seen = json.loads(run["feedback"] or "{}")
    feedback = None
    if seen.get("comments") or seen.get("ci"):
        feedback = {"comments": seen.get("comments") or [], "ci": seen.get("ci") or [],
                    "until": seen.get("until") or run["checked_at"] or _iso(_now())}
    cur = conn.execute(
        """INSERT INTO runs (task_id, executor, model, status, queued_at, kind, project, parent_run_id, branch,
                             pr_url, sub_pr_urls, feedback, base_branch, depends_on_run)
           VALUES (?, ?, ?, 'queued', ?, 'repair', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run["task_id"], name, cfg.raw["executors"][name]["model"], _iso(_now()), run["project"], run["id"],
         run["branch"], run["pr_url"], run["sub_pr_urls"], json.dumps(feedback, ensure_ascii=False) if feedback else None,
         base, stacked["id"] if rebase else None),
    )
    work = t("runs.repair_work")
    if feedback:
        work = t("runs.repair_work_feedback", feedback=feedback_line(feedback))
    if rebase:
        work = t("runs.repair_rebase", run_id=stacked["id"]) + " " + work
    return t("runs.repair_queued", run_id=run["id"], name=name, new_id=cur.lastrowid, work=work)


def runner_offline(runner_seen: str | None) -> str | None:
    if runner_seen and datetime.fromisoformat(runner_seen) > _now() - timedelta(minutes=RUNNER_OFFLINE_MINUTES):
        return None
    when = (t("runs.offline_since", time=datetime.fromisoformat(runner_seen).astimezone().strftime(t("fmt.time_day")))
            if runner_seen else t("runs.never_seen"))
    return t("runs.offline", when=when)


def clear(conn: sqlite3.Connection) -> int:
    """Annulla i run ancora in coda (quelli in esecuzione finiscono)."""
    rows = conn.execute("SELECT id, briefing_item_id FROM runs WHERE status = 'queued'").fetchall()
    for r in rows:
        conn.execute("UPDATE runs SET status = 'cancelled', finished_at = ? WHERE id = ?", (_iso(_now()), r["id"]))
        conn.execute("UPDATE briefing_items SET status = 'approved' WHERE id = ?", (r["briefing_item_id"],))
    return len(rows)


def _fail_running(conn: sqlite3.Connection, rows: list[sqlite3.Row], error: str) -> list[int]:
    """Segna falliti i run indicati; le voci del briefing tornano approvate."""
    for r in rows:
        conn.execute("UPDATE runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                     (error, _iso(_now()), r["id"]))
        conn.execute("UPDATE briefing_items SET status = 'approved' WHERE id = ?", (r["briefing_item_id"],))
    return [r["id"] for r in rows]


def expire_stale(conn: sqlite3.Connection, hours: float) -> list[int]:
    """Run in esecuzione senza notizie dal runner da troppo tempo: falliti, la voce torna approvata."""
    limit = _iso(_now() - timedelta(hours=hours))
    rows = conn.execute(
        "SELECT id, briefing_item_id FROM runs WHERE status = 'running' AND COALESCE(heartbeat_at, started_at) < ?",
        (limit,),
    ).fetchall()
    return _fail_running(conn, rows, t("runs.stale_error", hours=hours))


def expire_orphaned(conn: sqlite3.Connection, active: list[int] | None = None) -> list[int]:
    """Il runner chiede lavoro con l'elenco dei run che sta ancora eseguendo (`active`): un run 'running' che non è
    nell'elenco è orfano (runner riavviato o crollato), fallito subito e la voce torna approvata. Un runner vecchio
    non manda l'elenco e chiede lavoro solo quando è libero: `active` vuoto, ogni run 'running' è orfano."""
    keep = {int(i) for i in active or []}
    rows = [r for r in conn.execute("SELECT id, briefing_item_id FROM runs WHERE status = 'running'").fetchall()
            if r["id"] not in keep]
    return _fail_running(conn, rows, t("runs.orphan_error"))


RATE_LIMIT_WAIT_MINUTES = 10  # una CLI che ha appena preso un rate limit in un run in corso non parte in un altro run


def _family(cfg: Config, project: str | None) -> set:
    """Il progetto, i suoi submodule e il progetto che lo include: lavorano sugli stessi repo."""
    if project is None:
        return set()
    family = {project, *((cfg.projects.get(project, {}).get("submodules") or {}).values())}
    parent = cfg.submodule_parent(project)
    if parent:
        family.add(parent[0])
    return family


def _cli(cfg: Config, executor: str | None) -> str | None:
    return cfg.raw["executors"].get(executor or "", {}).get("cli")


def _running(conn: sqlite3.Connection, cfg: Config) -> list[dict]:
    """I run in corso, con quello che serve a can_start."""
    since = _iso(_now() - timedelta(minutes=RATE_LIMIT_WAIT_MINUTES))
    rows = conn.execute(
        """SELECT r.id, r.kind, r.executor, r.parent_run_id, r.parallel_group, COALESCE(r.project, t.project) AS project,
                  EXISTS (SELECT 1 FROM quota_events q WHERE q.run_id = r.id AND q.kind = 'rate_limited'
                          AND q.observed_at >= ?) AS rate_limited
           FROM runs r JOIN tasks t ON t.id = r.task_id WHERE r.status = 'running'""", (since,)).fetchall()
    return [dict(r) | {"cli": _cli(cfg, r["executor"]) if r["kind"] != "realign" else None} for r in rows]


def can_start(cfg: Config, run: dict, running: list[dict]) -> str | None:
    """None se il run in coda può partire accanto a quelli in corso, altrimenti il motivo dell'attesa
    (chiave in runs.wait.*). Deterministico, niente LLM.

    `run` ha: kind, project (quello in cui gira, dopo submodule_parent), cli (None se non lancia agenti),
    parent_run_id. Ogni voce di `running` ha anche id e rate_limited."""
    if run.get("wait_on"):  # dipendenza del planner non ancora pronta (anche senza niente in corso)
        return "dependency"
    if not running:
        return None
    if len(running) >= int(cfg.runner.get("max_parallel") or 1):
        return "full"
    projects = cfg.projects
    if not projects.get(run["project"], {}).get("parallel_safe", True) \
            or any(not projects.get(r["project"], {}).get("parallel_safe", True) for r in running):
        return "parallel_safe"
    family = _family(cfg, run["project"])
    group = run.get("parallel_group")
    if any((r["project"] in family or run["project"] in _family(cfg, r["project"]))
           and not (group and r.get("parallel_group") == group) for r in running):  # "insieme": l'owner li vuole così
        return "same_project"
    if run.get("parent_run_id") and any(run["parent_run_id"] in (r["id"], r["parent_run_id"]) for r in running):
        return "same_parent"
    cli = run.get("cli")
    if cli:
        same = [r for r in running if r["cli"] == cli]
        if same and not providers.can(cli, "concurrent"):
            return "cli"
        if any(r["rate_limited"] for r in same):
            return "quota"
    return None


def _effective_executor(conn: sqlite3.Connection, cfg: Config, row: sqlite3.Row) -> str:
    """L'esecutore con cui il run partirebbe: come _avoid_exhausted, ma senza scrivere niente."""
    if row["kind"] not in ("agent", "repair", "plan") or not _avoided(conn, cfg, row["executor"]):
        return row["executor"]
    return _next_executor(conn, cfg, row["executor"], {row["executor"]}) or row["executor"]


APPROVED = ("approved_as_is", "approved_with_changes")


def dependencies(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """Stato delle dipendenze proposte dal planner per un run in coda (voce del briefing con depends_on).

    - wait_on: run da aspettare (dipendenza in coda o in corso, piano in attesa, PR aperta con submodule, più PR
      aperte da cui impilarsi);
    - stack: il run su cui impilarsi (PR aperta senza submodule): si parte dal suo branch;
    - unsatisfied: task da cui dipende ma senza lavoro utile (mai lanciato, fallito, PR scartata): si parte dalla base
      normale e l'owner viene avvisato.
    I run lanciati "insieme" all'owner ignorano le dipendenze fra loro. Un run che ha già una base (scelta
    dall'owner, o impilato prima di un ritentativo) non le guarda più."""
    out = {"wait_on": [], "stack": None, "unsatisfied": []}
    if row["kind"] not in ("agent", "plan") or not row["depends_on"] or row["base_branch"] or row["depends_on_run"]:
        return out
    group, stacks = row["parallel_group"], []
    for task_id in json.loads(row["depends_on"]):
        runs_of = conn.execute(
            """SELECT r.*, t.external_id FROM runs r JOIN tasks t ON t.id = r.task_id
               WHERE r.task_id = ? AND r.kind IN ('agent', 'plan', 'repair') ORDER BY r.id DESC""",
            (task_id,)).fetchall()
        if group and any(r["parallel_group"] == group for r in runs_of):
            continue
        active = next((r for r in runs_of if r["status"] in ("queued", "running")), None)
        latest = runs_of[0] if runs_of else None
        agent = next((r for r in runs_of if r["kind"] == "agent"), None)
        if active:
            out["wait_on"].append(active["id"])
        elif latest and latest["kind"] == "plan" and latest["plan_state"] in ("proposed", "approved"):
            out["wait_on"].append(latest["id"])  # piano in attesa dell'owner: il lavoro vero arriverà
        elif agent and agent["status"] == "pr_open" and agent["branch"]:
            if json.loads(agent["sub_pr_urls"] or "[]"):
                out["wait_on"].append(agent["id"])  # il puntatore del submodule punterebbe a un commit non mergiato
            else:
                stacks.append(agent)
        elif agent and agent["status"] == "done" and agent["pr_outcome"] in APPROVED:
            continue  # mergiata: è già nella base
        else:
            ref = latest["external_id"] if latest else conn.execute(
                "SELECT external_id FROM tasks WHERE id = ?", (task_id,)).fetchone()["external_id"]
            out["unsatisfied"].append(ref)
    if len(stacks) > 1:  # una sola base possibile: si aspetta che le altre vengano mergiate
        out["wait_on"] += [s["id"] for s in stacks[1:]]
    if stacks and not out["wait_on"]:
        out["stack"] = stacks[0]
    return out


def _candidate(cfg: Config, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    parent = cfg.submodule_parent(row["task_project"])
    executor = _effective_executor(conn, cfg, row)
    return {"kind": row["kind"], "project": parent[0] if parent else row["project"],
            "cli": _cli(cfg, executor) if row["kind"] != "realign" else None, "parent_run_id": row["parent_run_id"],
            "parallel_group": row["parallel_group"], "wait_on": dependencies(conn, row)["wait_on"]}


def waiting(conn: sqlite3.Connection, cfg: Config) -> dict[int, str]:
    """run_id -> motivo (già tradotto) dei run in coda che adesso non possono partire, per "coda"."""
    running = _running(conn, cfg)
    out = {}
    for row in conn.execute(CLAIM_SELECT + " ORDER BY r.id").fetchall():
        candidate = _candidate(cfg, conn, row)
        reason = can_start(cfg, candidate, running)
        if reason:
            out[row["id"]] = t(f"runs.wait.{reason}", cli=candidate["cli"] or "",
                               run=", #".join(map(str, candidate["wait_on"])))
    return out


CLAIM_SELECT = """SELECT r.id, r.executor, r.model, r.kind, r.parent_run_id, r.feedback, r.fallback_from, r.base_branch,
                  r.plan_of, r.plan_note, r.owner_note, r.parallel_group, r.depends_on_run, i.depends_on,
                  t.source, t.external_id,
                  COALESCE(r.project, t.project) AS project, t.project AS task_project,
                  COALESCE(i.proposed_type, t.type) AS type, t.title, t.description, t.url, t.labels
           FROM runs r JOIN tasks t ON t.id = r.task_id LEFT JOIN briefing_items i ON i.id = r.briefing_item_id
           WHERE r.status = 'queued'"""


def claim(conn: sqlite3.Connection, cfg: Config) -> dict | None:
    """Prende il run in coda più vecchio che può partire accanto a quelli in corso (can_start, al massimo
    runner.max_parallel insieme) e lo passa a 'running'."""
    running = _running(conn, cfg)
    if len(running) >= int(cfg.runner.get("max_parallel") or 1):
        return None
    row = next((r for r in conn.execute(CLAIM_SELECT + " ORDER BY r.id").fetchall()
                if can_start(cfg, _candidate(cfg, conn, r), running) is None), None)
    if row is None:
        return None
    now = _iso(_now())
    conn.execute(
        "UPDATE runs SET status = 'running', started_at = ?, heartbeat_at = ?, step = 'worktree' WHERE id = ?",
        (now, now, row["id"]),
    )
    if row["kind"] in ("agent", "repair", "plan"):
        row = _avoid_exhausted(conn, cfg, row)
    # Il task di un submodule (my-lib) gira nel worktree del progetto che lo include.
    parent = cfg.submodule_parent(row["task_project"])
    run_project = parent[0] if parent else row["project"]
    if parent:
        conn.execute("UPDATE runs SET project = ? WHERE id = ?", (run_project, row["id"]))
    project = cfg.projects[run_project]
    # La base di progetto vale solo per i run nuovi: una riparazione (o un riallineamento) di un run creato prima
    # della migrazione con base NULL resta sul branch di default, dove sta la sua PR.
    deps = dependencies(conn, row)
    stacked = deps["stack"]
    if stacked and row["kind"] == "plan":
        pass  # il piano legge il codice impilato, ma non lo salva: all'approvazione il run vero rivaluta le dipendenze
    elif stacked:  # impilato sulla PR aperta da cui dipende: parte dal suo branch, la PR punta lì
        conn.execute("UPDATE runs SET base_branch = ?, depends_on_run = ? WHERE id = ?",
                     (stacked["branch"], stacked["id"], row["id"]))
    base_branch = (stacked["branch"] if stacked else row["base_branch"]) or (
        project.get("base_branch") if row["kind"] == "agent" else None)
    if base_branch and base_branch != row["base_branch"] and not stacked:  # base di progetto: la salvo, "ripara N" riusa la stessa
        conn.execute("UPDATE runs SET base_branch = ? WHERE id = ?", (base_branch, row["id"]))
    job = {
        "run_id": row["id"],
        "base_branch": base_branch,
        "kind": row["kind"],
        "branch": f"agent/{row['id']}",
        "executor": {"name": row["executor"], **cfg.raw["executors"].get(row["executor"], {})},
        "task": {k: row[k] for k in ("source", "external_id", "type", "title", "description", "url")}
                | {"labels": json.loads(row["labels"])},
        "project": {"name": run_project} | {
            k: project.get(k) for k in ("path", "workdir", "setup", "build", "test", "copy_files", "agent_notes")
        },
        "repo": _repo(project),
        "submodules": [
            {"path": path, "project": child, "repo": _repo(cfg.projects[child]),
             "local_path": cfg.projects[child]["path"], "test": cfg.projects[child].get("test"),
             "copy_files": cfg.projects[child].get("copy_files") or []}
            for path, child in (project.get("submodules") or {}).items()
        ],
        "limits": cfg.runner,
        "owner": cfg.owner_name,
        "locale": get_locale(),  # il runner scrive prompt, PR e messaggi d'errore in questa lingua
    }
    if row["owner_note"]:
        job["user_note"] = row["owner_note"]
    if stacked:
        job["stacked_on"] = {"run_id": stacked["id"], "pr_url": stacked["pr_url"], "branch": stacked["branch"]}
    if deps["unsatisfied"]:  # il server lo manda all'owner, non va al runner
        job["dependency_notice"] = t("runs.dependency_unsatisfied", run_id=row["id"],
                                     refs=", ".join(html.escape(short_ref(r)) for r in deps["unsatisfied"]))
    if parent:
        job["focus"] = parent[1]
    if row["kind"] == "realign":  # niente agente: sposta il submodule della PR app sul commit mergiato
        origin = conn.execute("SELECT branch, pr_url, sub_pr_state FROM runs WHERE id = ?",
                              (row["parent_run_id"],)).fetchone()
        merged = [(url, s) for url, s in json.loads(origin["sub_pr_state"] or "{}").items() if s["state"] == "merged"]
        job["realign"] = {"branch": origin["branch"], "pr_url": origin["pr_url"], "parent_run_id": row["parent_run_id"],
                          "path": job["submodules"][0]["path"], "sub_pr": merged[0][0], "sha": merged[0][1]["sha"]}
        return job
    if row["kind"] == "plan":  # sola lettura: niente review, niente build; eventuale piano da rivedere
        job["plan"] = {"timeout_minutes": (cfg.raw.get("plan") or {}).get("timeout_minutes", 15)}
        origin = conn.execute("SELECT plan_text, plan_note FROM runs WHERE id = ?", (row["plan_of"],)).fetchone() \
            if row["plan_of"] else None
        if origin:
            job["plan"].update(previous=origin["plan_text"], note=row["plan_note"])
        return job
    if row["kind"] == "agent" and row["plan_of"]:  # piano approvato dall'owner: l'agente lo segue
        job["approved_plan"] = conn.execute("SELECT plan_text FROM runs WHERE id = ?",
                                            (row["plan_of"],)).fetchone()["plan_text"]
    reviewer = cfg.counterpart(row["executor"])
    if reviewer:
        job["reviewer"] = {"name": reviewer, **cfg.raw["executors"][reviewer]}
        conn.execute("UPDATE runs SET reviewer_executor = ?, reviewer_model = ? WHERE id = ?",
                     (reviewer, job["reviewer"]["model"], row["id"]))
    job["review"] = cfg.review
    if row["kind"] == "repair":
        origin = conn.execute("SELECT branch, pr_url, sub_pr_urls FROM runs WHERE id = ?",
                              (row["parent_run_id"],)).fetchone()
        job["repair"] = {"branch": origin["branch"], "pr_url": origin["pr_url"], "parent_run_id": row["parent_run_id"],
                         "sub_pr_urls": json.loads(origin["sub_pr_urls"] or "[]")}
        if row["feedback"]:
            feedback = json.loads(row["feedback"])
            job["repair"]["feedback"] = {"comments": feedback["comments"], "ci": feedback["ci"]}
        if row["depends_on_run"]:  # ribasa la PR impilata: toglie i commit della PR già mergiata
            below = conn.execute("SELECT pr_url, branch FROM runs WHERE id = ?", (row["depends_on_run"],)).fetchone()
            number = re.search(r"/pull/(\d+)", below["pr_url"] or "")
            if number:
                job["repair"]["rebase"] = {"pr_number": int(number.group(1)), "pr_url": below["pr_url"],
                                           "branch": below["branch"]}
    if row["fallback_from"]:  # sostituito in partenza: quota dell'esecutore scelto esaurita
        job["fallback_from"] = row["fallback_from"]
    elif row["kind"] == "agent" and row["parent_run_id"]:  # ripartito con la controparte per quota esaurita
        job["fallback_from"] = conn.execute("SELECT executor FROM runs WHERE id = ?",
                                            (row["parent_run_id"],)).fetchone()["executor"]
    return job


def cli_exhausted(conn: sqlite3.Connection, cli: str) -> str | None:
    """Fino a quando (ISO UTC) la quota di una CLI risulta esaurita, dall'ultimo rate limit visto dal runner.

    Vale per i provider senza /usage (capability "quota"; agy lo legge il runner prima di partire): senza
    orario di reset nel messaggio si evita per QUOTA_AVOID_HOURS."""
    row = conn.execute("""SELECT reset_at, observed_at FROM quota_events WHERE cli = ?
                          AND kind IN ('rate_limited', 'exhausted') ORDER BY id DESC LIMIT 1""", (cli,)).fetchone()
    if row is None:
        return None
    try:
        until = (datetime.fromisoformat(row["reset_at"]) if row["reset_at"]
                 else datetime.fromisoformat(row["observed_at"]) + timedelta(hours=QUOTA_AVOID_HOURS))
    except ValueError:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return _iso(until) if until > _now() else None


def claude_exhausted(conn: sqlite3.Connection) -> str | None:
    """Compat: come cli_exhausted(conn, "claude")."""
    return cli_exhausted(conn, "claude")


def _avoided(conn: sqlite3.Connection, cfg: Config, executor: str) -> bool:
    """True se l'esecutore va evitato in partenza: provider senza /usage con un rate limit ancora attivo."""
    cli = cfg.raw["executors"].get(executor, {}).get("cli")
    return not providers.can(cli, "quota") and cli_exhausted(conn, cli) is not None


def _next_executor(conn: sqlite3.Connection, cfg: Config, root: str, tried: set[str]) -> str | None:
    """Il primo esecutore del fallback di `root` non ancora provato e non da evitare."""
    return next((e for e in cfg.fallback_chain(root) if e not in tried and not _avoided(conn, cfg, e)), None)


def _avoid_exhausted(conn: sqlite3.Connection, cfg: Config, row: sqlite3.Row) -> sqlite3.Row | dict:
    """Run con la quota ancora esaurita: parte subito col primo esecutore disponibile del fallback, invece di
    fallire dopo il setup."""
    executors = cfg.raw["executors"]
    if not _avoided(conn, cfg, row["executor"]):
        return row
    other = _next_executor(conn, cfg, row["executor"], {row["executor"]})
    if other is None:
        return row
    conn.execute("""UPDATE runs SET executor = ?, model = ?, fallback_from = ?, rate_limit_hits = rate_limit_hits + 1
                    WHERE id = ?""", (other, executors[other]["model"], row["executor"], row["id"]))
    conn.execute("""UPDATE briefing_items SET final_executor = ?, decision_source = 'quota_fallback'
                    WHERE id = (SELECT briefing_item_id FROM runs WHERE id = ?)""", (other, row["id"]))
    return {**dict(row), "executor": other, "model": executors[other]["model"], "fallback_from": row["executor"]}


def _repo(project: dict) -> str | None:
    repos = (project.get("sources") or {}).get("github") or []
    return repos[0] if repos else None


def update(conn: sqlite3.Connection, run_id: int, payload: dict, cfg: Config | None = None) -> dict:
    """Aggiornamento dal runner: step, stato finale, PR, esito test, review, quote. Restituisce il run aggiornato.

    Con `cfg`, un run fallito per quota esaurita riparte una volta con la controparte (altra famiglia)."""
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise RunError(t("runs.err.update_no_run", run_id=run_id))
    if run["status"] != "running":
        raise RunError(t("runs.err.update_not_running", run_id=run_id, status=run["status"]))
    status = payload.get("status", "running")
    if status not in ("running", "pr_open", "done", "failed", "cancelled"):
        raise RunError(t("runs.err.bad_status", status=status))
    if payload.get("step") is not None and payload["step"] not in STEPS:
        raise RunError(t("runs.err.bad_step", step=payload["step"]))

    now = _now()
    fields = {"heartbeat_at": _iso(now), "status": status}
    for key in ("step", "branch", "worktree_path", "pr_url", "summary", "tests", "error"):
        if payload.get(key) is not None:
            fields[key] = str(payload[key])[:4000]
    if payload.get("sub_pr_urls"):
        fields["sub_pr_urls"] = json.dumps([str(u) for u in payload["sub_pr_urls"]][:5])
    if payload.get("log"):
        add_log(conn, run_id, list(payload["log"]))
    if run["kind"] == "plan" and status == "done" and payload.get("plan"):
        fields["plan_text"], fields["plan_state"] = str(payload["plan"])[:12000], "proposed"
    if payload.get("review") is not None:
        fields["review"] = json.dumps(payload["review"], ensure_ascii=False)[:8000]
        fields["review_rounds"] = int(payload["review"].get("rounds") or 0)
    if payload.get("rate_limited"):
        fields["rate_limit_hits"] = run["rate_limit_hits"] + 1
    for q in payload.get("quota") or []:
        conn.execute(
            """INSERT INTO quota_events (cli, bucket, kind, remaining_fraction, reset_at, run_id, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (q["cli"], q.get("bucket"), q["kind"], q.get("remaining_fraction"), q.get("reset_at"), run_id, _iso(now)),
        )
    if status != "running":
        fields["finished_at"] = _iso(now)
        started = datetime.fromisoformat(run["started_at"])
        fields["duration_seconds"] = int((now - started).total_seconds())
    conn.execute(
        f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), run_id)
    )
    if status == "done" and run["kind"] == "plan":
        pass  # piano pronto: la voce resta in coda finché l'owner approva, modifica o scarta
    elif status != "running":  # pr_open: la voce è fatta; failed e cancelled: torna approvata, si può rilanciare
        conn.execute(
            "UPDATE briefing_items SET status = ? WHERE id = ?",
            ("done" if status == "pr_open" else "approved", run["briefing_item_id"]),
        )
    if run["kind"] == "repair" and status == "done":
        # il feedback passato all'agente è sistemato: contano solo i commenti scritti dopo
        since = json.loads(run["feedback"])["until"] if run["feedback"] else run["queued_at"]
        conn.execute("UPDATE runs SET feedback_since = ? WHERE id = ?", (since, run["parent_run_id"]))
    if run["kind"] == "repair" and status == "done" and run["depends_on_run"]:
        # ribasata: da ora la PR del run originale sta sulla base normale, e le prossime riparazioni partono da lì
        conn.execute("UPDATE runs SET base_branch = ? WHERE id = ?", (run["base_branch"], run["parent_run_id"]))
    if run["kind"] == "repair" and status == "done" and payload.get("sub_pr_urls"):
        # una PR nuova su un submodule toccato solo nella riparazione: la segue il run originale (riallineamento)
        conn.execute("UPDATE runs SET sub_pr_urls = ? WHERE id = ?", (fields["sub_pr_urls"], run["parent_run_id"]))
    if status == "failed" and payload.get("rate_limited") and cfg is not None:
        _fallback(conn, cfg, dict(run))
    return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


def _fallback(conn: sqlite3.Connection, cfg: Config, run: dict) -> None:
    """Quota esaurita o CLI non pronta: il run riparte col prossimo esecutore del fallback dell'esecutore di
    partenza, saltando quelli già provati o da evitare. Finito l'elenco, il run resta fallito."""
    if run["kind"] != "agent":
        return
    tried, current, root = set(), run, run["executor"]
    while current:
        tried.add(current["executor"])
        if current["fallback_from"]:
            tried.add(current["fallback_from"])
        root = current["fallback_from"] or current["executor"]
        current = (conn.execute("SELECT * FROM runs WHERE id = ? AND kind = 'agent'",
                                (current["parent_run_id"],)).fetchone() if current["parent_run_id"] else None)
    other = _next_executor(conn, cfg, root, tried)
    if other is None:
        return
    conn.execute(
        """INSERT INTO runs (task_id, briefing_item_id, executor, model, status, queued_at, kind, parent_run_id,
                             base_branch, owner_note, plan_of, parallel_group, depends_on_run)
           VALUES (?, ?, ?, ?, 'queued', ?, 'agent', ?, ?, ?, ?, ?, ?)""",
        (run["task_id"], run["briefing_item_id"], other, cfg.raw["executors"][other]["model"], _iso(_now()), run["id"],
         run["base_branch"], run.get("owner_note"), run.get("plan_of"), run.get("parallel_group"),
         run.get("depends_on_run")),
    )
    conn.execute("UPDATE briefing_items SET status = 'queued', final_executor = ? WHERE id = ?",
                 (other, run["briefing_item_id"]))


def _describe(conn: sqlite3.Connection, run: dict) -> tuple[str, str]:
    task = conn.execute("SELECT external_id, url, title FROM tasks WHERE id = ?", (run["task_id"],)).fetchone()
    ref = f'<a href="{html.escape(task["url"], quote=True)}">{html.escape(short_ref(task["external_id"]))}</a>'
    return ref, html.escape(task["title"], quote=False)


def notification(conn: sqlite3.Connection, run: dict) -> str | None:
    """Messaggio Telegram per un cambio di stato del run, None se già notificato."""
    status = run["status"]
    realign, repairing, planning = (run.get("kind") == "realign", run.get("kind") == "repair",
                                    run.get("kind") == "plan")
    if status == run.get("notified_status") or status not in ("running", "pr_open", "failed", "cancelled") + (
            ("done",) if realign or repairing or planning else ()):
        return None
    ref, title = _describe(conn, run)
    e = lambda s: html.escape(str(s), quote=False)  # noqa: E731
    head = dict(run_id=run["id"], ref=ref, title=title)  # intestazione comune ai messaggi del run
    who = t("notif.who_realign") if realign else (
        t("notif.who_repair", executor=e(run["executor"]), parent=run["parent_run_id"])
        if repairing and status == "running" else e(run["executor"]))
    tests = _tests_label(run)
    if planning and status in ("running", "done"):
        what = t("notif.plan_reviewing") if run.get("plan_of") else t("notif.plan_preparing")
        if status == "running":
            text = t("notif.plan_running", **head, who=who, what=what)
        else:
            plan_text = run.get("plan_text") or t("notif.plan_empty")
            questions = plantext.extract_plan_questions(plan_text)
            q_block = ("\n\n" + t("notif.plan_questions_head") + "\n" + "\n".join(f"• {e(q)}" for q in questions)
                       if questions else "")
            cmd_footer = (t("notif.plan_ready_commands_questions", run_id=run["id"]) if questions
                          else t("notif.plan_ready_commands", run_id=run["id"]))
            text = (t("notif.plan_ready", **head, who=who)
                    + (t("notif.plan_revised") if run.get("plan_of") else "") + "\n\n"
                    + plantext.to_telegram_html(plan_text,
                                                f"{run['project']}-{run['id']}" if run.get("project") else None)
                    + q_block
                    + "\n\n" + cmd_footer)
    elif repairing and status != "running":
        subs = json.loads(run.get("sub_pr_urls") or "[]")
        if status == "done":
            text = t("notif.repair_done", **head, parent=run["parent_run_id"])
            for url in subs:
                text += t("notif.sub_pr", url=e(url))
            text += t("notif.repair_pr_app", url=e(run["pr_url"] or "-"), who=who, tests=tests)
            if run.get("review"):
                text += "\n    " + _review_line(json.loads(run["review"]))
            text += t("notif.repair_details")
        elif status == "cancelled":
            text = t("notif.repair_cancelled", **head, step=e(run["step"] or "-"))
        else:
            text = t("notif.repair_failed", **head, step=e(run["step"] or "-"), error=e((run["error"] or "-")[:500]),
                     parent=run["parent_run_id"])
    elif realign and status == "done":
        text = t("notif.realign_done", **head, url=e(run["pr_url"] or "-"), tests=tests)
    elif realign and status == "failed":
        text = t("notif.realign_failed", **head, step=e(run["step"] or "-"), error=e((run["error"] or "-")[:500]))
    elif status == "running":
        text = t("notif.running", **head, who=who)
        if run.get("plan_of") and run.get("kind") == "agent":
            text += t("notif.follows_plan", plan_id=run["plan_of"])
        if run.get("fallback_from"):
            text += t("notif.quota_still_out", executor=e(run["fallback_from"]))
        if repairing and run.get("feedback"):
            text += t("notif.to_fix", feedback=feedback_line(json.loads(run["feedback"])))
    elif status == "cancelled":
        text = t("notif.cancelled", **head, step=e(run["step"] or "-"))
    elif status == "pr_open":
        subs = json.loads(run.get("sub_pr_urls") or "[]")
        text = t("notif.pr_open", **head)
        for url in subs:  # prima il submodule: è la PR da mergiare per prima
            text += t("notif.sub_pr", url=e(url))
        text += t("notif.pr_line", kind=t("notif.pr_kind_app" if subs else "notif.pr_kind_draft"),
                  url=e(run["pr_url"] or "-"), who=who, tests=tests)
        if run.get("review"):
            text += "\n    " + _review_line(json.loads(run["review"]))
        if run.get("parent_run_id"):
            text += t("notif.restarted_from", parent=run["parent_run_id"])
        elif run.get("fallback_from"):
            text += t("notif.done_by_counterpart", executor=e(run["fallback_from"]))
        if subs:
            text += t("notif.merge_order")
    else:
        err = run.get("error") or ""
        no_change_messages = {
            messages("it").get("runner.err.no_change"),
            messages("en").get("runner.err.no_change"),
        }
        orphaned = err in {messages("it").get("runs.orphan_error"), messages("en").get("runs.orphan_error"),
                           t("runs.orphan_error")}
        needs_info = (
            run.get("step") == "agent"
            and bool(run.get("summary"))
            and (err in no_change_messages or err == t("runner.err.no_change"))
        )
        if needs_info:
            item = (conn.execute("SELECT position FROM briefing_items WHERE id = ?", (run["briefing_item_id"],)).fetchone()
                    if run.get("briefing_item_id") else None)
            summary = run["summary"] if len(run["summary"]) <= 3000 else run["summary"][:3000] + "…"
            text = (t("notif.needs_info", **head)
                    + "\n\n" + t("notif.agent_summary", summary=e(summary)))
            if item:
                text += "\n\n" + t("notif.needs_info_commands", pos=item["position"])
        elif orphaned:
            item = (conn.execute("SELECT position FROM briefing_items WHERE id = ?", (run["briefing_item_id"],)).fetchone()
                    if run.get("briefing_item_id") else None)
            text = t("notif.orphaned", **head, step=e(run["step"] or "-"))
            if run.get("worktree_path"):
                text += t("notif.orphaned_worktree", path=e(run["worktree_path"]))
            if item:
                text += "\n\n" + t("notif.orphaned_commands", pos=item["position"])
        else:
            text = t("notif.failed", **head, step=e(run["step"] or "-"), error=e(err[:500]))
            if run["rate_limit_hits"]:
                retry = conn.execute("SELECT id, executor FROM runs WHERE parent_run_id = ? AND kind = 'agent'",
                                     (run["id"],)).fetchone()
                text += (t("notif.quota_retry", who=who, executor=e(retry["executor"]), run_id=retry["id"])
                         if retry else t("notif.quota_out"))
            if run.get("summary"):  # l'agente si è fermato spiegando perché: è il risultato utile del run
                summary = run["summary"] if len(run["summary"]) <= 3000 else run["summary"][:3000] + "…"
                text += t("notif.agent_summary", summary=e(summary))
    conn.execute("UPDATE runs SET notified_status = ? WHERE id = ?", (status, run["id"]))
    return text


def _tests_label(run: dict) -> str:
    known = {"passed": "notif.tests_passed", "failed": "notif.tests_failed", "skipped": "notif.tests_skipped"}
    return t(known[run["tests"]]) if run["tests"] in known else (run["tests"] or "-")


def _review_line(review: dict) -> str:
    """"review agy: 2 problemi, corretti" per il ✅."""
    who = html.escape(str(review.get("reviewer") or "-"), quote=False)
    if review.get("error"):
        return t("notif.review_not_run", who=who, error=html.escape(str(review["error"])[:120], quote=False))
    issues = review.get("issues") or []
    if not issues:
        return t("notif.review_clean", who=who)
    serious = sum(1 for i in issues if i.get("severity") in ("blocker", "major"))
    fixed = t("notif.review_fixed") if review.get("rounds") and serious else ""
    return t("notif.review_summary", who=who, count=len(issues), serious=serious, fixed=fixed)


def status_text(conn: sqlite3.Connection, runner_seen: str | None, cfg: Config | None = None) -> str:
    rows = conn.execute(
        """SELECT r.*, t.external_id, t.url, t.title FROM runs r JOIN tasks t ON t.id = r.task_id
           WHERE r.status IN ('queued', 'running') OR r.finished_at >= ? OR r.plan_state = 'proposed'
           ORDER BY r.id""",
        (_iso(_now() - timedelta(hours=24)),),
    ).fetchall()
    icons = {"queued": "⏳", "running": "▶️", "pr_open": "✅", "done": "🏁", "failed": "❌", "cancelled": "⏹"}
    waits = waiting(conn, cfg) if cfg else {}
    lines = [t("status.title")]
    if runner_seen:
        lines[0] += t("status.runner_seen", time=datetime.fromisoformat(runner_seen).astimezone().strftime(t("fmt.time_day")))
    else:
        lines[0] += t("status.runner_never")
    for r in rows:
        ref = f'<a href="{html.escape(r["url"], quote=True)}">{html.escape(short_ref(r["external_id"]))}</a>'
        extra = r["pr_url"] if r["status"] in ("pr_open", "done") else (r["step"] if r["status"] == "running"
                                                                         else waits.get(r["id"], ""))
        icon = icons.get(r["status"], "?")
        if r["kind"] == "plan":
            icon = "📝" if r["status"] == "done" and r["plan_state"] == "proposed" else icon
            states = {"proposed": t("status.plan_proposed", run_id=r["id"]), "approved": t("status.plan_approved"),
                      "revised": t("status.plan_revised"), "discarded": t("status.plan_discarded")}
            extra = states.get(r["plan_state"]) or (
                t("status.plan") if not extra else t("status.plan_extra", extra=extra))
        lines.append(f"{icon} #{r['id']} {ref} {html.escape(r['executor'])}"
                     + (f" · {html.escape(extra, quote=False)}" if extra else ""))
    if len(lines) == 1:
        lines.append(t("status.empty"))
    if any(r["status"] in ("queued", "running") for r in rows) and (offline := runner_offline(runner_seen)):
        lines.append(offline)
    return "\n".join(lines)
