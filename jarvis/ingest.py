"""Upsert dei task normalizzati da n8n.

Il payload è uno snapshot completo dei task aperti per le sorgenti in `complete_sources`:
i task aperti nel DB che non compaiono più nello snapshot vengono chiusi.
"""

import hashlib
import json
import sqlite3

from jarvis.checks import record_ingest
from jarvis.config import Config
from jarvis.i18n import t as tr

SOURCES = ("jira", "github")
REQUIRED = ("source", "external_id", "container", "title", "url", "source_status")

# Tipo dedotto dalla sorgente; se nessuna regola corrisponde lo decide il planner.
JIRA_TYPES = {"bug": "bugfix"}
GITHUB_LABEL_TYPES = {
    "bug": "bugfix",
    "enhancement": "feature",
    "documentation": "docs",
    "dependencies": "dipendenze",
}


class IngestError(ValueError):
    pass


def _project(cfg: Config, task: dict) -> str | None:
    if task["source"] == "jira":
        return cfg.project_for_jira(task["container"])
    return cfg.project_for_repo(task["container"])


def _type(task: dict) -> str | None:
    if task["source"] == "jira":
        return JIRA_TYPES.get((task.get("issue_type") or "").lower())
    for label in task["labels"]:
        if label.lower() in GITHUB_LABEL_TYPES:
            return GITHUB_LABEL_TYPES[label.lower()]
    return None


def _hash(task: dict) -> str:
    parts = [task["title"], task.get("description") or "", task["source_status"], *sorted(task["labels"])]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _validate(payload: dict) -> list[dict]:
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise IngestError(tr("ingest.err.tasks_list"))
    for i, t in enumerate(tasks):
        missing = [f for f in REQUIRED if not t.get(f)]
        if missing:
            raise IngestError(tr("ingest.err.missing_fields", index=i, missing=missing))
        if t["source"] not in SOURCES:
            raise IngestError(tr("ingest.err.unknown_source", index=i, source=t["source"]))
        t.setdefault("labels", [])
    complete = payload.get("complete_sources", [])
    if any(s not in SOURCES for s in complete):
        raise IngestError(tr("ingest.err.complete_sources", complete=complete))
    if complete and not tasks and not payload.get("allow_empty"):
        # Uno snapshot vuoto chiuderebbe tutto: di solito è un errore a monte.
        raise IngestError(tr("ingest.err.empty_snapshot"))
    return tasks


def ingest(conn: sqlite3.Connection, cfg: Config, payload: dict, now: str) -> dict:
    tasks = _validate(payload)
    identities = {"github": cfg.owner_github, **payload.get("identities", {})}
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "closed": 0}

    conn.execute("BEGIN")
    try:
        for t in tasks:
            content_hash = _hash(t)
            me = identities.get(t["source"])
            project = _project(cfg, t)
            eligible = int(
                t.get("created_by") == me
                or cfg.eligible_label in t["labels"]
                or (t["source"] in cfg.eligible_if_assigned and project is not None and t.get("assignee") == me)
            )
            row = conn.execute(
                "SELECT content_hash, is_open FROM tasks WHERE source = ? AND external_id = ?",
                (t["source"], t["external_id"]),
            ).fetchone()
            values = dict(
                source=t["source"],
                external_id=t["external_id"],
                project=project,
                type=_type(t),
                title=t["title"],
                description=t.get("description"),
                url=t["url"],
                source_status=t["source_status"],
                labels=json.dumps(t["labels"]),
                created_by=t.get("created_by"),
                assignee=t.get("assignee"),
                eligible=eligible,
                content_hash=content_hash,
                now=now,
            )
            if row is None:
                conn.execute(
                    """INSERT INTO tasks (source, external_id, project, type, title, description, url,
                           source_status, labels, created_by, assignee, eligible, content_hash,
                           first_seen_at, updated_at)
                       VALUES (:source, :external_id, :project, :type, :title, :description, :url,
                           :source_status, :labels, :created_by, :assignee, :eligible, :content_hash,
                           :now, :now)""",
                    values,
                )
                counts["inserted"] += 1
            elif row["content_hash"] != content_hash or not row["is_open"]:
                conn.execute(
                    """UPDATE tasks SET project = :project, type = :type, title = :title,
                           description = :description, url = :url, source_status = :source_status,
                           labels = :labels, created_by = :created_by, assignee = :assignee,
                           eligible = :eligible, content_hash = :content_hash, is_open = 1,
                           closed_at = NULL, updated_at = :now
                       WHERE source = :source AND external_id = :external_id""",
                    values,
                )
                counts["updated"] += 1
            else:
                # Il contenuto non è cambiato ma config o identità potrebbero sì.
                conn.execute(
                    """UPDATE tasks SET project = :project, eligible = :eligible, assignee = :assignee
                       WHERE source = :source AND external_id = :external_id""",
                    values,
                )
                counts["unchanged"] += 1

        for source in payload.get("complete_sources", []):
            seen = [t["external_id"] for t in tasks if t["source"] == source]
            placeholders = ",".join("?" * len(seen)) or "NULL"
            cur = conn.execute(
                f"""UPDATE tasks SET is_open = 0, closed_at = ?
                    WHERE source = ? AND is_open = 1 AND external_id NOT IN ({placeholders})""",
                (now, source, *seen),
            )
            counts["closed"] += cur.rowcount
            record_ingest(conn, source, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return counts
