"""Connessione SQLite, schema e migrazioni."""

import sqlite3
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent / "db"
SCHEMA = DB_DIR / "schema.sql"
MIGRATIONS = DB_DIR / "migrations"  # NNNN_nome.sql: portano user_version a NNNN


def connect(path: str | Path) -> sqlite3.Connection:
    # Transazioni esplicite; la connessione è condivisa tra thread dietro un lock (vedi server.App).
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
        conn.executescript(SCHEMA.read_text())
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for file in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        target = int(file.name[:4])
        if target > version:
            conn.executescript(f"BEGIN;\n{file.read_text()}\nPRAGMA user_version = {target};\nCOMMIT;")
            version = target
