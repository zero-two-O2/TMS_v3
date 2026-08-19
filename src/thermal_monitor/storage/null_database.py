"""
storage.null_database -- Null database implementation for offline/testing.

Provides a no-op database implementation that returns empty results
for all queries, allowing the processing pipeline to work without
a real SQL Server connection.
"""

from __future__ import annotations

from typing import Any, Optional

try:
    import pyodbc
    _HAS_PYODBC = True
except ImportError:
    pyodbc = None  # type: ignore
    _HAS_PYODBC = False


class NullCursor:
    """Null cursor that returns empty results."""

    def __init__(self) -> None:
        self.description = None

    def execute(self, sql: str, params: tuple = ()) -> None:
        pass

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[tuple]:
        return []

    def close(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class NullConnection:
    """Null connection that returns null cursors."""

    def cursor(self) -> NullCursor:
        return NullCursor()

    def close(self) -> None:
        pass

    @property
    def timeout(self) -> int:
        return 30

    @timeout.setter
    def timeout(self, value: int) -> None:
        pass


class NullDatabase:
    """Null database implementation for offline/testing.

    Returns empty results for all queries without connecting to SQL Server.
    """

    def __init__(self) -> None:
        self._connected = True

    def connect(self) -> NullConnection:
        return NullConnection()

    def disconnect(self) -> None:
        pass

    @property
    def is_connected(self) -> bool:
        return True

    def execute(self, sql: str, params: tuple = ()) -> NullCursor:
        return NullCursor()

    def fetch_one(self, sql: str, params: tuple = ()) -> None:
        return None

    def fetch_all(self, sql: str, params: tuple = ()) -> list[tuple]:
        return []

    def transaction(self):
        """Null transaction context manager."""
        from contextlib import contextmanager

        @contextmanager
        def null_transaction():
            cursor = NullCursor()
            try:
                yield cursor
            finally:
                cursor.close()

        return null_transaction()