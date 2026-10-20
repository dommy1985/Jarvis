"""Comandi GitHub da Telegram: interpretazione, validazione, anteprima, esecuzione.

`claude -p` riceve solo il messaggio dell'owner e l'elenco di repo e board, senza strumenti
né token: restituisce un'azione strutturata. Jarvis la valida contro projects.yaml e i
progetti GitHub reali, mostra l'anteprima e la esegue solo dopo "sì".
"""

import html
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from jarvis import i18n
from jarvis.config import Config
from jarvis.github import GitHub, GitHubError
from jarvis.i18n import t, words

ACTIONS = ("create_issue", "add_to_project", "set_status", "assign")
JIRA_KEY = re.compile(r"\b([A-Za-z][A-Za-z0-9]+-\d+)\b")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


class CommandError(ValueError):
    """Azione non valida o non eseguibile: il messaggio va mostrato all'owner."""


def _nullable(kind: str) -> dict:
    return {"type": [kind, "null"]}


def schema() -> dict:
    strings = {"type": "array", "items": {"type": "string"}}
    fields = {
        "action": {"type": "string", "enum": [*ACTIONS, "none"]},
        "repo": _nullable("string"),
        "issue_number": _nullable("integer"),
        "title": _nullable("string"),
        "body": _nullable("string"),
        "labels": strings,
        "assignees": strings,
        "project_owner": _nullable("string"),
        "project_number": _nullable("integer"),
        "status": _nullable("string"),
        "reply": {"type": "string"},
    }
    return {"type": "object", "additionalProperties": False, "required": list(fields), "properties": fields}


def tickets(conn: sqlite3.Connection, text: str) -> list[dict]:
    """Ticket Jira importati citati nel testo, con le issue GitHub che li citano già (per clonarli)."""
    keys = sorted({k.upper() for k in JIRA_KEY.findall(text)})
    found = []
    for key in keys:
        row = conn.execute(
            "SELECT external_id, project, title, url, source_status FROM tasks WHERE source = 'jira' AND external_id = ?",
            (key,),
        ).fetchone()
        if row is None:
            continue
        clones = conn.execute(
            "SELECT url FROM tasks WHERE source = 'github' AND (title LIKE ? OR description LIKE ?)",
            (f"%{key}%", f"%{key}%"),
        ).fetchall()
        found.append({"key": key, "project": row["project"], "title": row["title"], "url": row["url"],
                      "status": row["source_status"], "github_issues": [c["url"] for c in clones]})
    return found


def message(cfg: Config, text: str, projects: list[dict], previous: dict | None = None,
            tickets: list[dict] | None = None) -> str:
    repos = []
    for repo in cfg.writable_repos():
        project = cfg.project_for_repo(repo)
        repos.append(f"{repo} ({project})" if project else repo)
    context = {
        "repos": repos,
        "projects": [
            {"owner": p["owner"], "number": p["number"], "title": p["title"], "statuses": list(p["statuses"])}
            for p in projects
        ],
        "default_assignee": cfg.owner_github,
    }
    if tickets:
        context["tickets"] = tickets
    if previous:  # il messaggio risponde alla domanda fatta nel turno precedente
        context["previous"] = previous
    return json.dumps(context, ensure_ascii=False, indent=1) + "\n\n<message>\n" + text + "\n</message>"


def fetch_projects(cfg: Config, gh: GitHub) -> list[dict]:
    return [p for owner in cfg.commands["write_owners"] for p in gh.projects(owner)]


def interpret(cfg: Config, text: str, projects: list[dict], runner, previous: dict | None = None,
              tickets: list[dict] | None = None) -> dict:
    executor = cfg.raw["executors"][cfg.commands["executor"]]
    return runner(cfg.fill(i18n.prompt("commands")), message(cfg, text, projects, previous, tickets), schema(),
                  executor["model"], cfg.commands["timeout_seconds"])


def validate(cfg: Config, raw: dict, projects: list[dict]) -> dict:
    """Output del modello -> azione eseguibile. Solleva CommandError con un messaggio per l'owner."""
    action = raw.get("action")
    if action == "none" or action not in ACTIONS:
        raise CommandError(raw.get("reply") or t("commands.err.not_understood"))

    repos = {r.lower(): r for r in cfg.writable_repos()}
    repo = repos.get(str(raw.get("repo") or "").lower())
    if repo is None:
        raise CommandError(t("commands.err.repo_not_writable", repo=raw.get("repo"), repos=", ".join(repos.values())))

    result = {"action": action, "repo": repo, "issue_number": None, "title": None, "body": "",
              "labels": [], "assignees": [], "project": None, "status": None}
    if action == "create_issue":
        title = (raw.get("title") or "").strip()
        if not title or len(title) > 256:
            raise CommandError(t("commands.err.title_needed"))
        result.update(title=title, body=(raw.get("body") or "").strip()[:5000])
        labels = [str(label).strip() for label in raw.get("labels") or [] if str(label).strip()]
        if len(labels) > 5 or any(len(label) > 50 for label in labels):
            raise CommandError(t("commands.err.labels"))
        result["labels"] = labels
    else:
        number = raw.get("issue_number")
        if not isinstance(number, int) or number <= 0:
            raise CommandError(t("commands.err.issue_number"))
        result["issue_number"] = number

    assignees = [str(a).strip().lstrip("@") for a in raw.get("assignees") or []]
    if any(not LOGIN.fullmatch(a) for a in assignees) or len(assignees) > 5:
        raise CommandError(t("commands.err.assignees", names=", ".join(assignees)))
    if action == "assign" and not assignees:
        raise CommandError(t("commands.err.assign_to_whom"))
    result["assignees"] = assignees

    if raw.get("project_number") is not None:
        owner = str(raw.get("project_owner") or "").lower()
        match = [p for p in projects if p["number"] == raw["project_number"] and (not owner or p["owner"].lower() == owner)]
        if len(match) != 1:
            raise CommandError(t("commands.err.board_not_found", owner=raw.get("project_owner"),
                                 number=raw["project_number"]))
        result["project"] = {k: match[0][k] for k in ("owner", "number", "title", "id", "status_field")}
        if raw.get("status"):
            statuses = {name.lower(): (name, oid) for name, oid in match[0]["statuses"].items()}
            status = statuses.get(str(raw["status"]).lower())
            if status is None:
                raise CommandError(t("commands.err.status_not_found", status=raw["status"], title=match[0]["title"],
                                     statuses=", ".join(match[0]["statuses"])))
            result["status"] = {"name": status[0], "id": status[1]}
    elif raw.get("status"):
        raise CommandError(t("commands.err.status_needs_board"))
    if action in ("add_to_project", "set_status") and result["project"] is None:
        raise CommandError(t("commands.err.which_board"))
    if action == "set_status" and result["status"] is None:
        raise CommandError(t("commands.err.which_status"))
    return result


def preview(action: dict, minutes: int) -> str:
    e = lambda s: html.escape(str(s), quote=False)  # noqa: E731
    target = f"<code>{e(action['repo'])}</code>"
    if action["issue_number"]:
        target = f"<code>{e(action['repo'])}#{action['issue_number']}</code>"
    lines = [t("commands.preview.head", verb=t("commands.verb." + action["action"]), target=target)]
    if action["title"]:
        lines.append(t("commands.preview.title", title=e(action["title"])))
    if action["body"]:
        body = action["body"] if len(action["body"]) <= 600 else action["body"][:600] + "…"
        lines.append(t("commands.preview.body", body=e(body)))
    if action["labels"]:
        lines.append(t("commands.preview.labels", labels=e(", ".join(action["labels"]))))
    if action["assignees"]:
        lines.append(t("commands.preview.assignees", assignees=e(", ".join(action["assignees"]))))
    if action["project"]:
        board = f"{e(action['project']['title'])}"
        if action["status"]:
            board += f" → {e(action['status']['name'])}"
        lines.append(t("commands.preview.board", board=board))
    lines += ["", t("commands.preview.confirm", minutes=minutes)]
    return "\n".join(lines)


def execute(gh: GitHub, action: dict) -> dict:
    """Esegue l'azione. Se fallisce a metà, GitHubError porta con sé quanto già fatto in `partial`."""
    done: dict = {}
    try:
        repo, number = action["repo"], action["issue_number"]
        if action["action"] == "create_issue":
            issue = gh.create_issue(repo, action["title"], action["body"], action["labels"], action["assignees"])
            done.update(url=issue["html_url"], number=issue["number"])
            content_id, items = issue["node_id"], []
        else:
            issue = gh.issue(repo, number)
            done["url"] = issue["url"]
            content_id, items = issue["id"], issue["projectItems"]["nodes"]
            if action["assignees"]:
                gh.add_assignees(repo, number, action["assignees"])
                done["assigned"] = action["assignees"]
        project = action["project"]
        if project:
            item = next((i["id"] for i in items if i["project"]["id"] == project["id"]), None)
            if item is None:
                item = gh.add_to_project(project["id"], content_id)
                done["added_to"] = project["title"]
            if action["status"]:
                gh.set_status(project["id"], item, project["status_field"], action["status"]["id"])
                done["status"] = action["status"]["name"]
    except GitHubError as e:
        e.partial = done
        raise
    return done


def imported_task(cfg: Config, action: dict, result: dict) -> dict | None:
    """Issue appena creata in forma di task per l'ingest: così si può far partire subito, senza
    aspettare il prossimo ingest orario."""
    if action["action"] != "create_issue" or not result.get("number"):
        return None
    return {"source": "github", "external_id": f"{action['repo']}#{result['number']}", "container": action["repo"],
            "title": action["title"], "description": action["body"] or None, "url": result["url"],
            "source_status": "OPEN", "labels": action["labels"], "created_by": cfg.owner_github,
            "assignee": action["assignees"][0] if action["assignees"] else None}


# --- stato nel database ---

def _now() -> datetime:
    return datetime.now(timezone.utc)


def save(conn: sqlite3.Connection, message_id: int, text: str, action: dict | None, error: str | None) -> bool:
    """Registra la richiesta; una nuova azione in attesa sostituisce le precedenti. False se già vista."""
    now = _now().isoformat(timespec="seconds")
    if action is not None:
        conn.execute("UPDATE commands SET status = 'cancelled', decided_at = ? WHERE status = 'pending'", (now,))
    cur = conn.execute(
        """INSERT OR IGNORE INTO commands (telegram_message_id, text, action, status, error, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (message_id, text, json.dumps(action) if action else None,
         "pending" if action else "rejected", error, now),
    )
    return cur.rowcount == 1


def seen(conn: sqlite3.Connection, message_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM commands WHERE telegram_message_id = ? OR confirm_message_id = ?", (message_id, message_id)
    ).fetchone() is not None


def recent_question(conn: sqlite3.Connection, minutes: int = 10) -> dict | None:
    """Ultima richiesta respinta con una domanda, se è l'ultimo comando e recente: il messaggio dopo è la risposta."""
    row = conn.execute("SELECT text, status, error, created_at FROM commands ORDER BY id DESC LIMIT 1").fetchone()
    since = (_now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    if row is None or row["status"] != "rejected" or row["created_at"] < since or not row["error"]:
        return None
    return {"request": row["text"], "your_reply": row["error"]}


def has_pending(conn: sqlite3.Connection, minutes: int) -> bool:
    since = (_now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT 1 FROM commands WHERE status = 'pending' AND created_at >= ?", (since,)
    ).fetchone() is not None


def is_yes(word: str) -> bool:
    return word in words("yes")


def is_no(word: str) -> bool:
    return word in words("no")


def decision(word: str) -> bool | None:
    """Risposta a un'azione in attesa ("sì, crea", "no grazie"): True/False se il messaggio inizia con sì o no, altrimenti None."""
    first = re.split(r"[\s,;:!.]+", word, maxsplit=1)[0]
    return True if first in words("yes") else False if first in words("no") or word.startswith(
        tuple(words("no_prefixes"))) else None


def take_pending(conn: sqlite3.Connection, confirm_message_id: int, minutes: int, status: str) -> dict | None:
    """Chiude l'azione in attesa più recente (status 'executed' o 'cancelled') e la restituisce."""
    now = _now()
    conn.execute(
        "UPDATE commands SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
        ((now - timedelta(minutes=minutes)).isoformat(timespec="seconds"),),
    )
    row = conn.execute(
        "SELECT id, action FROM commands WHERE status = 'pending' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE commands SET status = ?, confirm_message_id = ?, decided_at = ? WHERE id = ?",
        (status, confirm_message_id, now.isoformat(timespec="seconds"), row["id"]),
    )
    return {"id": row["id"], **json.loads(row["action"])}


def finish(conn: sqlite3.Connection, command_id: int, result: dict, error: str | None) -> None:
    conn.execute(
        "UPDATE commands SET status = ?, result = ?, error = ? WHERE id = ?",
        ("failed" if error else "executed", json.dumps(result), error, command_id),
    )
