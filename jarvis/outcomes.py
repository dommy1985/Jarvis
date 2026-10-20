"""Fase 3: esiti delle PR aperte dai run e riallineamento del submodule dopo il merge del framework.

Jarvis guarda ogni `outcomes.check_minutes` le PR dei run in stato pr_open:
- PR dell'app mergiata o chiusa: il run passa a 'done' con l'esito (approvata così, con modifiche, scartata).
  "Con modifiche" = nella PR c'è almeno un commit che non viene dal runner (senza "Run Jarvis #").
- PR del submodule mergiata mentre quella dell'app è aperta: mette in coda un run 'realign' (niente agente)
  che sposta il submodule della PR app sul commit mergiato. Una volta sola: se fallisce va fatto a mano.
- PR ancora aperte con commenti di review nuovi o CI rossa: propone su Telegram "ripara N", che passa
  commenti e check falliti all'agente. Non parte mai da solo; un nuovo avviso solo se il feedback cambia.
"""

import hashlib
import html
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from jarvis.briefing import short_ref
from jarvis.github import GitHub, GitHubError
from jarvis import i18n
from jarvis.i18n import t

log = logging.getLogger(__name__)
PR_URL = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
RUNNER_MARK = "Run Jarvis #"
# Commenti che il runner stesso scrive sulle PR (gh con l'identità dell'owner): non sono feedback. Il testo cambia
# col locale del run (marker.* nel catalogo), quindi vale quello di ogni locale installato.
def jarvis_comments() -> tuple[str, ...]:
    return i18n.every("marker.repair_comment") + i18n.every("marker.submodule_comment")


# Solo chi può scrivere sul repo (o un'app installata, es. un bot di review): su un repo pubblico chiunque
# può commentare, e il testo finisce nel prompt di un agente con la shell.
TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")
CI_FAILED = ("failure", "timed_out", "action_required")
MAX_COMMENTS = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pr(url: str) -> tuple[str, int]:
    m = PR_URL.fullmatch(url.strip())
    if m is None:
        raise GitHubError(t("outcomes.bad_pr_url", url=url))
    return m.group(1), int(m.group(2))


def pr_state(gh: GitHub, url: str) -> dict:
    data = gh.pull(*_pr(url))
    state = "merged" if data.get("merged") else data["state"]  # open, closed
    return {"state": state, "sha": data.get("merge_commit_sha") if state == "merged" else None,
            "head": (data.get("head") or {}).get("sha")}


def _trusted(item: dict) -> bool:
    return item.get("author_association") in TRUSTED or (item.get("user") or {}).get("type") == "Bot"


def pr_feedback(gh: GitHub, url: str, head: str | None, since: str) -> dict:
    """Commenti scritti dopo `since` e check falliti sull'ultimo commit di una PR aperta."""
    repo, number = _pr(url)
    comments = []
    for review in gh.pull_reviews(repo, number):
        if review.get("state") in ("CHANGES_REQUESTED", "COMMENTED") and (review.get("body") or "").strip():
            comments.append({**review, "created_at": review.get("submitted_at") or ""})
    comments += gh.pull_comments(repo, number) + gh.issue_comments(repo, number)
    found = []
    for c in comments:
        body = (c.get("body") or "").strip()
        if not body or (c.get("created_at") or "") <= since or not _trusted(c) or body.startswith(jarvis_comments()):
            continue
        item = {"id": c["id"], "pr": url, "author": (c.get("user") or {}).get("login") or "?",
                "at": c["created_at"], "text": body[:1500]}
        if c.get("path"):
            item["file"] = c["path"] + (f":{c['line']}" if c.get("line") else "")
        found.append(item)
    found.sort(key=lambda c: c["at"])
    ci = []
    if head:
        try:  # un token senza permesso sui check non deve fermare i commenti
            for check in gh.check_runs(repo, head):
                if check.get("conclusion") in CI_FAILED:
                    out = check.get("output") or {}
                    ci.append({"pr": url, "sha": head, "name": check.get("name") or "?",
                               "conclusion": check["conclusion"], "url": check.get("html_url"),
                               "summary": " ".join(filter(None, [out.get("title"), out.get("summary")]))[:800]})
            for status in gh.statuses(repo, head):
                if status.get("state") in ("failure", "error"):
                    ci.append({"pr": url, "sha": head, "name": status.get("context") or "?",
                               "conclusion": status["state"], "url": status.get("target_url"),
                               "summary": (status.get("description") or "")[:800]})
        except GitHubError as e:
            log.warning("CI di %s non letta: %s", url, e)
    return {"comments": found[-MAX_COMMENTS:], "ci": ci}


def _signature(feedback: dict) -> str:
    """Cambia con un commento nuovo o un check fallito su un commit nuovo, non con un push qualsiasi."""
    key = [sorted(c["id"] for c in feedback["comments"]),
           sorted((c["pr"], c["sha"], c["name"], c["conclusion"]) for c in feedback["ci"])]
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]


def feedback_line(feedback: dict | None) -> str:
    """"2 commenti di review, CI rossa (build)" per Telegram; "" se non c'è niente."""
    if not feedback:
        return ""
    parts = []
    if n := len(feedback.get("comments") or []):
        authors = sorted({c["author"] for c in feedback["comments"]})
        parts.append(t("outcomes.comments_one" if n == 1 else "outcomes.comments_many", count=n,
                       authors=html.escape(", ".join(authors), quote=False)))
    if feedback.get("ci"):
        names = sorted({c["name"] for c in feedback["ci"]})
        parts.append(t("outcomes.ci_red", names=html.escape(", ".join(names), quote=False)))
    return ", ".join(parts)


def changed_by_others(gh: GitHub, url: str) -> bool:
    return any(RUNNER_MARK not in c["commit"]["message"] for c in gh.pull_commits(*_pr(url)))


def refresh(conn: sqlite3.Connection, gh: GitHub) -> list[str]:
    """Aggiorna gli esiti e mette in coda i riallineamenti. Restituisce i messaggi per Telegram."""
    messages = []
    for run in conn.execute(
        """SELECT r.*, t.external_id, t.url AS task_url FROM runs r JOIN tasks t ON t.id = r.task_id
           WHERE r.status = 'pr_open' AND r.kind = 'agent' AND r.pr_url IS NOT NULL ORDER BY r.id"""
    ).fetchall():
        try:
            subs = json.loads(run["sub_pr_state"] or "{}")
            for url in json.loads(run["sub_pr_urls"] or "[]"):
                if subs.get(url, {}).get("state") not in ("merged", "closed"):
                    subs[url] = pr_state(gh, url)
            app = pr_state(gh, run["pr_url"])
            outcome = None
            if app["state"] == "closed":
                outcome = "discarded"
            elif app["state"] == "merged":
                outcome = "approved_with_changes" if changed_by_others(gh, run["pr_url"]) else "approved_as_is"
        except GitHubError as e:
            log.warning("esito run %s non letto: %s", run["id"], e)
            continue
        conn.execute("UPDATE runs SET sub_pr_state = ?, checked_at = ? WHERE id = ?",
                     (json.dumps(subs), _now(), run["id"]))
        if outcome:
            conn.execute("UPDATE runs SET status = 'done', pr_outcome = ? WHERE id = ?", (outcome, run["id"]))
            log.info("run %s: PR %s -> %s", run["id"], run["pr_url"], outcome)
            continue
        merged = [url for url, s in subs.items() if s["state"] == "merged"]
        already = conn.execute("SELECT 1 FROM runs WHERE parent_run_id = ? AND kind = 'realign'",
                               (run["id"],)).fetchone()
        if merged and not already:
            cur = conn.execute(
                """INSERT INTO runs (task_id, executor, model, status, queued_at, kind, project, parent_run_id)
                   VALUES (?, 'runner', '-', 'queued', ?, 'realign', ?, ?)""",
                (run["task_id"], _now(), run["project"], run["id"]),
            )
            ref = html.escape(short_ref(run["external_id"]), quote=False)
            messages.append(t("outcomes.submodule_merged", pr=html.escape(merged[0], quote=False), ref=ref,
                              run_id=cur.lastrowid))
        try:  # a parte: un errore sui commenti non deve fermare esiti e riallineamenti
            feedback = _collect(conn, gh, run, app, subs)
        except GitHubError as e:
            log.warning("feedback delle PR del run %s non letto: %s", run["id"], e)
            feedback = None
        if feedback is not None and (text := _propose_repair(conn, run, feedback)):
            messages.append(text)
    return messages


def _collect(conn: sqlite3.Connection, gh: GitHub, run: sqlite3.Row, app: dict, subs: dict) -> dict | None:
    """Feedback su tutte le PR ancora aperte del run; None se c'è già una riparazione in coda o in corso."""
    if conn.execute("SELECT 1 FROM runs WHERE parent_run_id = ? AND kind IN ('repair', 'realign') "
                    "AND status IN ('queued', 'running')", (run["id"],)).fetchone():
        return None
    since = run["feedback_since"] or run["finished_at"] or run["queued_at"]
    total = {"comments": [], "ci": []}
    for url, state in [(run["pr_url"], app), *subs.items()]:
        if state["state"] != "open":
            continue
        found = pr_feedback(gh, url, state.get("head"), since)
        total["comments"] += found["comments"]
        total["ci"] += found["ci"]
    return total


def _propose_repair(conn: sqlite3.Connection, run: sqlite3.Row, feedback: dict) -> str | None:
    """Salva il feedback sul run e, se è nuovo e non vuoto, propone "ripara N"."""
    previous = json.loads(run["feedback"] or "{}")
    feedback["sig"] = _signature(feedback)
    feedback["until"] = max([c["at"] for c in feedback["comments"]], default=None) or previous.get("until")
    conn.execute("UPDATE runs SET feedback = ? WHERE id = ?",
                 (json.dumps(feedback, ensure_ascii=False), run["id"]))
    if not (feedback["comments"] or feedback["ci"]) or feedback["sig"] == previous.get("sig"):
        return None
    ref = f'<a href="{html.escape(run["task_url"], quote=True)}">{html.escape(short_ref(run["external_id"]))}</a>'
    return t("outcomes.propose_repair", run_id=run["id"], ref=ref, feedback=feedback_line(feedback))


def stats_text(conn: sqlite3.Connection, days: int = 30) -> str:
    """Riepilogo per esecutore degli ultimi `days` giorni (run agente, non i riallineamenti)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT executor,
                  COUNT(*) AS runs,
                  SUM(pr_url IS NOT NULL) AS prs,
                  SUM(pr_outcome = 'approved_as_is') AS as_is,
                  SUM(pr_outcome = 'approved_with_changes') AS changed,
                  SUM(pr_outcome = 'discarded') AS discarded,
                  SUM(status = 'failed') AS failed,
                  SUM(rate_limit_hits > 0) AS quota,
                  SUM(review_rounds > 0) AS fixed,
                  AVG(CASE WHEN pr_url IS NOT NULL THEN duration_seconds END) AS avg_seconds
           FROM runs WHERE kind = 'agent' AND queued_at >= ? AND status != 'cancelled'
           GROUP BY executor ORDER BY runs DESC""",
        (since,),
    ).fetchall()
    if not rows:
        return t("outcomes.stats_none", days=days)
    lines = [t("outcomes.stats_title", days=days)]
    for r in rows:
        seconds = r["avg_seconds"] or 0
        avg = (t("outcomes.avg_min", value=round(seconds / 60)) if seconds >= 90
               else t("outcomes.avg_s", value=round(seconds))) if seconds else ""
        lines.append(t("outcomes.stats_line", executor=html.escape(r["executor"]), runs=r["runs"], prs=r["prs"] or 0,
                       as_is=r["as_is"] or 0, changed=r["changed"] or 0, discarded=r["discarded"] or 0,
                       failed=r["failed"] or 0, quota=r["quota"] or 0, fixed=r["fixed"] or 0, avg=avg))
    open_prs = conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'pr_open' AND kind = 'agent'").fetchone()[0]
    lines.append(t("outcomes.open_prs", count=open_prs))
    return "\n".join(lines)
