"""
storage.repositories.base -- Base repository classes.

Provides common CRUD operations for SQL Server repositories.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, fields
from typing import Any, Generic, TypeVar

from thermal_monitor.storage.database import Database

T = TypeVar("T")


@dataclass
class RepositoryResult(Generic[T]):
    """Result of a repository operation."""

    success: bool
    data: T | None = None
    error: str | None = None
    rows_affected: int = 0


class BaseRepository(ABC, Generic[T]):
    """Base class for SQL Server repositories."""

    def __init__(self, database: Database, table_name: str) -> None:
        self._db = database
        self._table_name = table_name

    @property
    def table_name(self) -> str:
        return self._table_name

    @abstractmethod
    def _to_entity(self, row: tuple) -> T:
        """Convert database row to entity."""
        ...

    @abstractmethod
    def _to_params(self, entity: T) -> tuple:
        """Convert entity to SQL parameters."""
        ...

    @abstractmethod
    def _get_columns(self) -> list[str]:
        """Get column names for this entity."""
        ...

    def _get_placeholders(self, count: int) -> str:
        return ",".join(["?"] * count)

    def insert(self, entity: T) -> RepositoryResult[T]:
        """Insert a new entity."""
        columns = self._get_columns()
        placeholders = self._get_placeholders(len(columns))
        params = self._to_params(entity)

        sql = f"INSERT INTO {self._table_name} ({','.join(columns)}) VALUES ({placeholders})"
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, params)
                # Get the inserted ID if using IDENTITY
                cursor.execute("SELECT SCOPE_IDENTITY()")
                row = cursor.fetchone()
                if row and row[0]:
                    # Would need to set ID on entity - depends on implementation
                    pass
            return RepositoryResult(success=True, data=entity, rows_affected=1)
        except Exception as e:
            return RepositoryResult(success=False, error=str(e))

    def update(self, entity: T, where_clause: str, where_params: tuple) -> RepositoryResult[T]:
        """Update an entity."""
        columns = self._get_columns()
        set_clause = ",".join([f"{col}=?" for col in columns])
        params = self._to_params(entity) + where_params

        sql = f"UPDATE {self._table_name} SET {set_clause} WHERE {where_clause}"
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, params)
                rows = cursor.rowcount
            return RepositoryResult(success=True, data=entity, rows_affected=rows)
        except Exception as e:
            return RepositoryResult(success=False, error=str(e))

    def delete(self, where_clause: str, where_params: tuple) -> RepositoryResult[int]:
        """Delete entities matching condition."""
        sql = f"DELETE FROM {self._table_name} WHERE {where_clause}"
        try:
            with self._db.transaction() as cursor:
                cursor.execute(sql, where_params)
                rows = cursor.rowcount
            return RepositoryResult(success=True, data=rows, rows_affected=rows)
        except Exception as e:
            return RepositoryResult(success=False, error=str(e))

    def find_by_id(self, entity_id: Any) -> RepositoryResult[T | None]:
        """Find entity by ID."""
        sql = f"SELECT * FROM {self._table_name} WHERE id = ?"
        try:
            row = self._db.fetch_one(sql, (entity_id,))
            if row:
                return RepositoryResult(success=True, data=self._to_entity(row))
            return RepositoryResult(success=True, data=None)
        except Exception as e:
            return RepositoryResult(success=False, error=str(e))

    def find_all(self, where_clause: str = "1=1", where_params: tuple = ()) -> RepositoryResult[list[T]]:
        """Find all entities matching condition."""
        sql = f"SELECT * FROM {self._table_name} WHERE {where_clause}"
        try:
            rows = self._db.fetch_all(sql, where_params)
            entities = [self._to_entity(row) for row in rows]
            return RepositoryResult(success=True, data=entities, rows_affected=len(entities))
        except Exception as e:
            return RepositoryResult(success=False, error=str(e))

    def execute_scalar(self, sql: str, params: tuple = ()) -> RepositoryResult[Any]:
        """Execute a scalar query."""
        try:
            result = self._db.fetch_one(sql, params)
            return RepositoryResult(success=True, data=result[0] if result else None)
        except Exception as e:
            return RepositoryResult(success=False, error=str(e))