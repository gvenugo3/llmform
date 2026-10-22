"""Structural PostgreSQL operation execution."""

from __future__ import annotations

import re
from typing import Any

from jsonschema import validate

from llmform.config.models import PostgresSource
from llmform.sources import Source, SourceError


class PostgresOperationSource(Source):
    """Executes declared named PostgreSQL operations with parameter binding and validation."""

    def __init__(
        self,
        config: PostgresSource,
        schemas: dict[str, dict[str, Any]],
        *,
        connection: Any = None,
        connect_fn: Any = None,
    ):
        self.config = config
        self.schemas = schemas
        self._connection = connection
        self._connect_fn = connect_fn

    def _get_connection(self) -> Any:
        if self._connection is not None:
            return self._connection
        if self._connect_fn is not None:
            return self._connect_fn()
        try:
            import psycopg  # type: ignore[import-untyped]
        except ImportError:
            try:
                import psycopg2 as psycopg  # type: ignore[import-untyped]
            except ImportError:
                raise SourceError(
                    "PostgreSQL driver (psycopg or psycopg2) is not installed; "
                    "install psycopg to execute Postgres sources"
                ) from None

        try:
            if self.config.dsn:
                return psycopg.connect(self.config.dsn)
            kwargs: dict[str, Any] = {}
            if self.config.host:
                kwargs["host"] = self.config.host
            if self.config.port:
                kwargs["port"] = self.config.port
            if self.config.database:
                kwargs["dbname"] = self.config.database
            if self.config.user:
                kwargs["user"] = self.config.user
            if self.config.password:
                kwargs["password"] = self.config.password
            if self.config.sslmode:
                kwargs["sslmode"] = self.config.sslmode
            return psycopg.connect(**kwargs)
        except Exception as exc:
            raise SourceError(f"failed to connect to PostgreSQL: {exc}") from exc

    def execute(self, operation: str, params: dict[str, Any], *, timeout: float) -> Any:
        if operation not in self.config.operations:
            raise SourceError(f"unknown operation {operation!r}")
        spec = self.config.operations[operation]

        # Enforce declared required parameters
        for name, param in spec.params.items():
            if param.required and name not in params:
                raise SourceError(f"missing required parameter: {name}")

        # Check for undeclared parameters supplied
        for name in params:
            if name not in spec.params:
                raise SourceError(f"unexpected parameter: {name}")

        # Enforce read-only if specified
        if spec.read_only:
            first_word = spec.query.strip().split()[0].upper() if spec.query.strip() else ""
            if first_word not in {"SELECT", "WITH", "EXPLAIN", "SHOW"}:
                raise SourceError(
                    f"Read-only operation cannot execute statement starting with {first_word!r}"
                )

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            query = spec.query

            # Determine placeholder style
            # If connection is sqlite3, it supports :param directly
            # For drivers using pyformat (%(name)s), convert :name to %(name)s if needed
            is_sqlite = type(conn).__module__.startswith("sqlite3")
            if not is_sqlite and ":" in query and "%(" not in query:
                query = re.sub(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", r"%(\1)s", query)

            try:
                cursor.execute(query, params)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                raw_rows = cursor.fetchall() if cursor.description else []
                rows: list[dict[str, Any]] = []
                for row in raw_rows:
                    if isinstance(row, dict):
                        rows.append(row)
                    elif hasattr(row, "keys"):
                        rows.append(dict(row))
                    else:
                        rows.append(dict(zip(columns, row, strict=False)))
            except Exception as exc:
                raise SourceError(f"query execution error: {exc}") from exc
            finally:
                cursor.close()
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(str(exc)) from exc

        return_schema = self.schemas.get(spec.returns)
        if return_schema is None:
            raise SourceError(f"missing schema for operation returns: {spec.returns}")

        schema_type = return_schema.get("type")
        if schema_type == "object":
            value: Any = rows[0] if rows else {}
        else:
            value = rows

        try:
            validate(value, return_schema)
        except Exception as exc:
            raise SourceError(f"operation result violates returns schema: {exc}") from exc

        return value
