"""
storage.sqlite_database -- Local SQLite persistence backend (Phase 9B).

Same surface as :class:`thermal_monitor.storage.database.Database`
(``connect`` / ``disconnect`` / ``transaction`` / ``execute`` /
``fetch_one`` / ``fetch_all`` / ``is_connected``) so the existing
repository abstraction works unchanged. SQLite is the development
backend; SQL Server remains the production backend. Callers must not
need to know which implementation is active.

Threading: one connection per owning thread. The database object keeps
a dedicated connection for the thread that first connects (usually a
worker thread); other threads get their own on-demand connections via
the transaction/execute path. Connections are never shared across
threads (``check_same_thread=True`` default). GUI threads must never
touch this object directly — use a worker (see
:mod:`thermal_monitor.storage.alarm_store`).

Secrets: never stored here. No credentials in SQLite.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class SqliteConfig:
    """Local SQLite connection configuration (no credentials)."""

    path: str
    connect_timeout_s: float = 10.0

    @property
    def resolved_path(self) -> Path:
        return Path(self.path).expanduser()


class SqliteDatabase:
    """SQLite database connection manager (file-backed, stdlib only)."""

    def __init__(self, config: SqliteConfig) -> None:
        if not config.path or not str(config.path).strip():
            raise ValueError("sqlite database path is required")
        self._config = config
        self._local = threading.local()
        self._main_conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._state = "closed"  # closed | connected | error
        self._detail = ""

    # -- lifecycle ------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Establish (or reuse) the calling thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error:
                self._drop_thread_conn()
        path = self._config.resolved_path
        try:
            if str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(path),
                timeout=self._config.connect_timeout_s,
                isolation_level=None,  # autocommit; transactions explicit
                check_same_thread=True,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
            if threading.current_thread() is threading.main_thread():
                self._main_conn = conn
            with self._lock:
                self._state = "connected"
                self._detail = ""
            return conn
        except Exception as exc:
            with self._lock:
                self._state = "error"
                self._detail = str(exc)
            logger.warning("SQLite connect failed path=%s: %s", path, exc)
            raise

    def disconnect(self) -> None:
        """Close the calling thread's connection (never raises)."""
        self._drop_thread_conn()

    def _drop_thread_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def shutdown(self) -> None:
        """Close all known connections (best effort, for app shutdown)."""
        self._drop_thread_conn()
        with self._lock:
            self._state = "closed"

    # -- queries --------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """Yield a cursor inside BEGIN/COMMIT (ROLLBACK on error)."""
        conn = self.connect()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            yield cursor
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            cursor.close()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a statement (caller owns the cursor)."""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        return cursor

    def fetch_one(self, sql: str, params: tuple = ()) -> tuple | None:
        cursor = self.execute(sql, params)
        try:
            row = cursor.fetchone()
            return tuple(row) if row is not None else None
        finally:
            cursor.close()

    def fetch_all(self, sql: str, params: tuple = ()) -> list[tuple]:
        cursor = self.execute(sql, params)
        try:
            return [tuple(r) for r in cursor.fetchall()]
        finally:
            cursor.close()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._state == "connected"

    @property
    def status(self) -> tuple[str, str]:
        """(state, detail) snapshot for status UI: never raises."""
        with self._lock:
            return self._state, self._detail

    # -- migrations -----------------------------------------------------

    @property
    def applied_versions(self) -> list[int]:
        try:
            rows = self.fetch_all("SELECT version FROM schema_version ORDER BY version")
            return [int(r[0]) for r in rows]
        except sqlite3.Error:
            return []

    def run_migrations(self, migrations_path: Path) -> list[Path]:
        """Apply pending ``NNN_*.sql`` scripts in order (versioned).

        Each script runs inside one transaction and its version row is
        recorded in ``schema_version``. Re-running is a no-op for
        applied versions. Never creates a fresh database per launch:
        existing files are migrated in place.
        """
        import time

        migrations_path = Path(migrations_path)
        with self.transaction() as cursor:
            cursor.execute(_SCHEMA_VERSION_TABLE)
        applied = set(self.applied_versions)
        done: list[Path] = []
        for script in sorted(migrations_path.glob("*.sql")):
            stem = script.stem
            digits = "".join(ch for ch in stem.split("_")[0] if ch.isdigit())
            if not digits:
                continue
            version = int(digits)
            if version in applied:
                continue
            sql = script.read_text(encoding="utf-8")
            with self.transaction() as cursor:
                cursor.executescript(sql)
                cursor.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, time.time()),
                )
            logger.info("SQLite migration applied: %s", script.name)
            done.append(script)
        return done


__all__ = ["SqliteConfig", "SqliteDatabase"]
