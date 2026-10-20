"""Avvisi da mostrare nel briefing: token in scadenza, ingest fermo, planner senza credenziali."""

import re
import sqlite3
from datetime import date as Date
from datetime import datetime, timedelta

from jarvis.config import Config
from jarvis.github import GitHub, GitHubError
from jarvis.i18n import t

TOKEN_WARN_DAYS = 7
INGEST_MISSED_RUNS = 3  # avviso dopo 3 ingest orari saltati
INGEST_SOURCES = ("jira", "github")
AUTH_ERROR = re.compile(r"auth|login|oauth|401|api key|credential", re.IGNORECASE)


def record(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value, value),
    )


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def record_ingest(conn: sqlite3.Connection, source: str, now: str) -> None:
    record(conn, f"last_ingest:{source}", now)


def missed_ingest_runs(last: datetime, now: datetime, cfg: Config) -> int:
    """Ingest programmati (alle ore di cfg.ingest_hours, ora di cfg.tz) tra `last` e `now`: di sera e di notte non ce ne sono."""
    slot = last.astimezone(cfg.tz).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end, missed = now.astimezone(cfg.tz), 0
    while slot <= end and missed < 100:
        missed += slot.hour in cfg.ingest_hours
        slot += timedelta(hours=1)
    return missed


def stale_ingest(conn: sqlite3.Connection, now: datetime, cfg: Config) -> list[str]:
    """Sorgenti senza un ingest riuscito nelle ultime ore (token Jira/GitHub di n8n scaduto, workflow fermo)."""
    notes = []
    for source in INGEST_SOURCES:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (f"last_ingest:{source}",)).fetchone()
        if row is None:
            notes.append(t("checks.no_ingest", source=source))
        elif missed_ingest_runs(datetime.fromisoformat(row["value"]), now, cfg) >= INGEST_MISSED_RUNS:
            last = datetime.fromisoformat(row["value"]).astimezone(now.tzinfo)
            notes.append(t("checks.stale_ingest", source=source, last=last.strftime(t("fmt.day_time"))))
    return notes


def github_token(github: GitHub | None, today: Date) -> list[str]:
    if github is None:
        return []
    try:
        expiration = github.token_expiration()
    except GitHubError as e:
        return [t("checks.token_broken", error=e)]
    if expiration is None:
        return []
    days = (Date.fromisoformat(expiration) - today).days
    if days > TOKEN_WARN_DAYS:
        return []
    when = t("checks.expired") if days < 0 else (t("checks.expires_today") if days == 0
                                                 else t("checks.expires_in", days=days))
    return [t("checks.token_expiring", when=when,
              date=Date.fromisoformat(expiration).strftime(t("fmt.date_full")))]


QUOTA_ERROR = re.compile(r"session limit|usage limit|limit reached|hit your .{0,20}limit", re.IGNORECASE)


def planner_hint(error: str) -> str | None:
    if QUOTA_ERROR.search(error):
        return t("checks.hint_quota")
    if AUTH_ERROR.search(error):
        return t("checks.hint_auth")
    return None
