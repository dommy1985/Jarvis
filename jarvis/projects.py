"""Comando `progetti`: i progetti su cui Jarvis opera, con task aperti e PR in corso."""

import html
import sqlite3

from jarvis.config import Config
from jarvis.i18n import t


def text(conn: sqlite3.Connection, cfg: Config, focus: list[str] | None = None) -> str:
    excluded = {s.lower() for s in cfg.planner["exclude_statuses"]}
    open_tasks: dict[str, int] = {}
    for r in conn.execute("SELECT project, source_status FROM tasks WHERE is_open = 1 AND eligible = 1"):
        if r["project"] and r["source_status"].lower() not in excluded:
            open_tasks[r["project"]] = open_tasks.get(r["project"], 0) + 1
    working = {r["project"]: (r["running"], r["prs"]) for r in conn.execute(
        """SELECT COALESCE(r.project, t.project) AS project,
                  SUM(r.status IN ('queued', 'running')) AS running, SUM(r.status = 'pr_open') AS prs
           FROM runs r JOIN tasks t ON t.id = r.task_id WHERE r.kind IN ('agent', 'plan', 'repair')
           GROUP BY 1""") if r["project"]}
    e = lambda s: html.escape(str(s), quote=False)  # noqa: E731
    active = {k: p for k, p in cfg.projects.items() if not p.get("paused")}
    paused = [k for k, p in cfg.projects.items() if p.get("paused")]
    lines = [t("projects.title", active=len(active)) + (t("projects.title_paused", paused=len(paused)) if paused else "")]
    if focus:
        lines.append(t("projects.focus", projects=e(", ".join(focus))))
    for name, project in active.items():
        sources = project.get("sources") or {}
        running, prs = working.get(name, (0, 0))
        head = t("projects.head", name=e(name), stack=e(project.get("stack", "-")), open=open_tasks.get(name, 0))
        if running:
            head += t("projects.running", count=running)
        if prs:
            head += t("projects.prs", count=prs)
        where = [*sources.get("github", []), *(f"Jira {k}" for k in sources.get("jira", []))]
        extras = []
        if parent := cfg.submodule_parent(name):
            extras.append(t("projects.runs_in", parent=parent[0]))
        if project.get("submodules"):
            extras.append(t("projects.includes", names=", ".join(project["submodules"].values())))
        if project.get("base_branch"):
            extras.append(t("projects.base", branch=project["base_branch"]))
        if project.get("plan_first"):
            extras.append(t("projects.plan_first"))
        lines.append(head + "\n    " + e(" · ".join([", ".join(where) or t("projects.no_source"), *extras])))
    if paused:
        lines.append(t("projects.paused", names=e(", ".join(paused))))
    lines.append(t("projects.footer"))
    return "\n".join(lines)
