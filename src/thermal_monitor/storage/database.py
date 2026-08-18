"""
storage.database -- SQL Server database connection and schema.

Provides database connection management and migration support.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import pyodbc
    _HAS_PYODBC = True
except ImportError:
    pyodbc = None  # type: ignore
    _HAS_PYODBC = False


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """SQL Server connection configuration."""

    server: str
    database: str
    username: str | None = None
    password: str | None = None
    driver: str = "ODBC Driver 17 for SQL Server"
    trust_server_certificate: bool = True
    connection_timeout: int = 30
    command_timeout: int = 30

    @property
    def connection_string(self) -> str:
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={self.server}",
            f"DATABASE={self.database}",
            f"TrustServerCertificate={'yes' if self.trust_server_certificate else 'no'}",
            f"Connection Timeout={self.connection_timeout}",
        ]
        if self.username and self.password:
            parts.append(f"UID={self.username}")
            parts.append(f"PWD={self.password}")
        else:
            parts.append("Trusted_Connection=yes")
        return ";".join(parts)


class Database:
    """SQL Server database connection manager."""

    def __init__(self, config: DatabaseConfig) -> None:
        if not _HAS_PYODBC:
            raise RuntimeError("pyodbc is required for SQL Server support. Install with: pip install pyodbc")
        self._config = config
        self._connection: Optional[pyodbc.Connection] = None

    def connect(self) -> pyodbc.Connection:
        """Establish database connection."""
        if self._connection is None:
            self._connection = pyodbc.connect(
                self._config.connection_string,
                timeout=self._config.connection_timeout,
            )
            self._connection.timeout = self._config.command_timeout
        return self._connection

    def disconnect(self) -> None:
        """Close database connection."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @contextmanager
    def transaction(self):
        """Context manager for database transactions."""
        conn = self.connect()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()

    def execute(self, sql: str, params: tuple = ()) -> pyodbc.Cursor:
        """Execute a SQL statement."""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return cursor

    def fetch_one(self, sql: str, params: tuple = ()) -> tuple | None:
        """Execute and fetch one row."""
        cursor = self.execute(sql, params)
        return cursor.fetchone()

    def fetch_all(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Execute and fetch all rows."""
        cursor = self.execute(sql, params)
        return cursor.fetchall()

    @property
    def is_connected(self) -> bool:
        return self._connection is not None


def run_migrations(database: Database, migrations_path: Path) -> None:
    """Run SQL migration scripts from a directory.

    Migration files should be named with numeric prefixes:
    001_initial_schema.sql
    002_add_camera_table.sql
    etc.
    """
    migration_files = sorted(migrations_path.glob("*.sql"))
    for migration_file in migration_files:
        sql = migration_file.read_text(encoding="utf-8")
        # Split by GO statements (SQL Server batch separator)
        batches = [batch.strip() for batch in sql.split("\nGO\n") if batch.strip()]
        with database.transaction() as cursor:
            for batch in batches:
                cursor.execute(batch)