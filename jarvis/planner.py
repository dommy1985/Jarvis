"""Planner: una chiamata al giorno a `claude -p` che produce il briefing.

Il planner riceve solo task aperti ed eligible, senza strumenti né file: deve restituire
un JSON conforme a `schema()`. Le regole di progetto vengono applicate dopo, qui.
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from jarvis import i18n, providers
from jarvis.config import Config
from jarvis.i18n import t

MAX_DESCRIPTION = 1500

# (system prompt, messaggio, schema JSON, modello, timeout) -> output strutturato
ClaudeRunner = Callable[[str, str, dict, str, int], dict]


class PlannerError(RuntimeError):
    pass


class BriefingLocked(PlannerError):
    """Il briefing del giorno è già stato inviato: rigenerarlo richiede force."""


def schema(cfg: Config) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "items"],
        "properties": {
            "summary": {"type": "string"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_id", "type", "executor", "estimate_hours", "rationale"],
                    "properties": {
                        "task_id": {"type": "integer"},
                        "type": {"type": "string", "enum": sorted(cfg.raw["routing"])},
                        "executor": {"type": "string", "enum": sorted(cfg.raw["executors"])},
                        "estimate_hours": {"type": "number", "minimum": 0},
                        "rationale": {"type": "string"},
                    },
                },
            },
        },
    }


def runner_for(cfg: Config, section: str = "planner") -> ClaudeRunner:
    """Runner strutturato del provider dell'esecutore configurato in `planner` o `commands`."""
    provider = providers.get(cfg.raw["executors"][cfg.raw[section]["executor"]]["cli"])

    def run(system: str, message: str, json_schema: dict, model: str, timeout: int) -> dict:
        try:
            return providers.call_structured(provider, system, message, json_schema, model, timeout)
        except providers.ProviderError as e:
            raise PlannerError(str(e)) from e

    return run


def _redacted(cfg: Config, task: sqlite3.Row) -> bool:
    return task["source"] == "jira" or bool(cfg.projects.get(task["project"], {}).get("redact_description"))


def candidates(conn: sqlite3.Connection, cfg: Config, today: str, focus: list[str] | None = None) -> list[dict]:
    excluded = {s.lower() for s in cfg.planner["exclude_statuses"]}
    only = cfg.focus_projects(focus)  # briefing concentrato: solo i task di questi progetti
    paused = {name for name, p in cfg.projects.items() if p.get("paused")}
    rows = conn.execute(
        "SELECT * FROM tasks WHERE is_open = 1 AND eligible = 1 ORDER BY updated_at DESC"
    ).fetchall()
    tasks = []
    for r in rows:
        if r["source_status"].lower() in excluded or r["project"] in paused:
            continue
        if only is not None and r["project"] not in only:
            continue
        task = {
            "task_id": r["id"],
            "ref": r["external_id"],
            "project": r["project"],
            "type": r["type"],
            "status": r["source_status"],
            "title": r["title"],
            "labels": json.loads(r["labels"]),
            "age_days": (datetime.fromisoformat(today) - datetime.fromisoformat(r["first_seen_at"][:10])).days,
        }
        if not _redacted(cfg, r) and r["description"]:
            task["description"] = r["description"][:MAX_DESCRIPTION]
        tasks.append(task)
        if len(tasks) == cfg.planner["max_tasks"]:
            break
    return tasks


def _message(cfg: Config, hours: float, tasks: list[dict], focus: list[str] | None = None) -> str:
    executors = {
        name: (t("planner.by_hand", owner=cfg.owner_name) if e.get("human") else f"{e['cli']} · {e['model']}")
        for name, e in cfg.raw["executors"].items()
    }
    context = {"available_hours": hours, "executors": executors, "routing": cfg.raw["routing"]}
    if focus:
        context["focus_projects"] = sorted(cfg.focus_projects(focus))
    return (
        json.dumps(context, ensure_ascii=False, indent=1)
        + "\n\n<tasks>\n" + json.dumps(tasks, ensure_ascii=False, indent=1) + "\n</tasks>"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plan(
    conn: sqlite3.Connection,
    cfg: Config,
    date: str,
    available_hours: float | None = None,
    force: bool = False,
    runner: ClaudeRunner | None = None,
    focus: list[str] | None = None,
) -> int:
    """Genera (o rigenera) il briefing del giorno e restituisce il suo id. Con `focus` (chiavi di progetto) il
    planner vede solo i task di quei progetti: vale solo per questo briefing, domani si riparte da tutti."""
    focus = list(focus) or None if focus else None
    hours = cfg.available_hours if available_hours is None else available_hours
    # I briefing dei giorni passati mai confermati non valgono più.
    conn.execute("UPDATE briefings SET status = 'expired' WHERE date < ? AND status IN ('draft', 'sent')", (date,))
    existing = conn.execute("SELECT id, status FROM briefings WHERE date = ?", (date,)).fetchone()
    if existing and existing["status"] in ("sent", "confirmed") and not force:
        raise BriefingLocked(t("planner.err.locked", date=date, status=existing["status"]))

    runner = runner or runner_for(cfg, "planner")
    tasks = candidates(conn, cfg, date, focus)
    executor = cfg.raw["executors"][cfg.planner["executor"]]
    output, error = None, None
    if tasks:
        try:
            output = runner(
                cfg.fill(i18n.prompt("planner")), _message(cfg, hours, tasks, focus), schema(cfg),
                executor["model"], cfg.planner["timeout_seconds"],
            )
        except PlannerError as e:
            error = str(e)
    else:
        where = t("planner.no_tasks_in", projects=", ".join(focus)) if focus else ""
        output = {"summary": t("planner.no_tasks", where=where), "items": []}

    by_id = {t["task_id"]: t for t in tasks}
    conn.execute("BEGIN")
    try:
        if existing:
            # I run già creati (piani, lavori) restano, solo scollegati dalle voci che sostituiamo.
            conn.execute("""UPDATE runs SET briefing_item_id = NULL WHERE briefing_item_id IN
                            (SELECT id FROM briefing_items WHERE briefing_id = ?)""", (existing["id"],))
            conn.execute("DELETE FROM briefing_items WHERE briefing_id = ?", (existing["id"],))
            conn.execute(
                """UPDATE briefings SET available_hours = ?, status = ?, planner_executor = ?, raw_output = ?,
                       error = ?, telegram_message_id = NULL, created_at = ?, sent_at = NULL, confirmed_at = NULL,
                       focus = ?
                   WHERE id = ?""",
                (hours, "failed" if error else "draft", cfg.planner["executor"],
                 json.dumps(output, ensure_ascii=False) if output else None, error, _now(),
                 json.dumps(focus) if focus else None, existing["id"]),
            )
            briefing_id = existing["id"]
        else:
            briefing_id = conn.execute(
                """INSERT INTO briefings (date, available_hours, status, planner_executor, raw_output, error, created_at,
                                         focus)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (date, hours, "failed" if error else "draft", cfg.planner["executor"],
                 json.dumps(output, ensure_ascii=False) if output else None, error, _now(),
                 json.dumps(focus) if focus else None),
            ).lastrowid

        position = 0
        for item in (output or {}).get("items", []):
            task = by_id.get(item["task_id"])
            if task is None:
                continue  # id inventato o task non passato al planner
            if conn.execute(
                "SELECT 1 FROM briefing_items WHERE briefing_id = ? AND task_id = ?", (briefing_id, task["task_id"])
            ).fetchone():
                continue  # duplicato
            executor_name, source = cfg.executor_for(task["project"], item["type"], item["executor"])
            position += 1
            conn.execute(
                """INSERT INTO briefing_items (briefing_id, task_id, position, proposed_type, proposed_executor,
                       estimate_hours, rationale, decision_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (briefing_id, task["task_id"], position, item["type"], executor_name,
                 item["estimate_hours"], item["rationale"], source),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if error:
        raise PlannerError(error)
    return briefing_id


def briefing(conn: sqlite3.Connection, date: str) -> dict | None:
    row = conn.execute("SELECT * FROM briefings WHERE date = ?", (date,)).fetchone()
    if row is None:
        return None
    items = conn.execute(
        """SELECT i.position, i.proposed_type, i.proposed_executor, i.final_executor, i.estimate_hours,
                  i.rationale, i.decision_source, i.status, t.external_id, t.project, t.title, t.url
           FROM briefing_items i JOIN tasks t ON t.id = i.task_id
           WHERE i.briefing_id = ? ORDER BY i.position""",
        (row["id"],),
    ).fetchall()
    raw = json.loads(row["raw_output"]) if row["raw_output"] else {}
    return {
        "id": row["id"],
        "date": row["date"],
        "status": row["status"],
        "available_hours": row["available_hours"],
        "focus": json.loads(row["focus"]) if row["focus"] else None,
        "summary": raw.get("summary"),
        "error": row["error"],
        "items": [dict(i) for i in items],
    }
