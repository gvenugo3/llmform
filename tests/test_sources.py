import io
import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from llmform.config.models import (
    HttpOperation,
    HttpSource,
    OperationParameter,
    PostgresOperation,
    PostgresSource,
)
from llmform.sources import SourceError
from llmform.sources.http import HttpOperationSource
from llmform.sources.mcp import McpSource
from llmform.sources.postgres import PostgresOperationSource


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def source() -> HttpOperationSource:
    return HttpOperationSource(
        HttpSource(
            type="http",
            base_url="https://api.example.test",
            operations={
                "lookup": HttpOperation(
                    method="GET", path="items/{id}", params={}, returns="return.json"
                )
            },
        ),
        {"return.json": {"type": "object", "required": ["ok"]}},
    )


def test_http_source_quotes_path_and_validates_result(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(
        "llmform.sources.http.urlopen",
        lambda request, **_: (
            seen.append(request.full_url) or Response(json.dumps({"ok": True}).encode())
        ),
    )
    assert source().execute("lookup", {"id": "a/b"}, timeout=1) == {"ok": True}
    assert seen == ["https://api.example.test/items/a%2Fb"]


def test_http_source_rejects_nonconforming_result(monkeypatch) -> None:
    monkeypatch.setattr("llmform.sources.http.urlopen", lambda *_args, **_kwargs: Response(b"{}"))
    with pytest.raises(SourceError, match="violates returns schema"):
        source().execute("lookup", {"id": "one"}, timeout=1)


def test_untrusted_mcp_command_never_spawns(monkeypatch) -> None:
    monkeypatch.setattr(
        "llmform.sources.mcp.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("untrusted command was launched"),
    )
    with pytest.raises(SourceError, match="untrusted"):
        McpSource(["untrusted-server"], trusted=False)


# --- Postgres Source Tests ---


def test_postgres_config_validation_offline() -> None:
    # Valid with dsn
    valid_dsn = PostgresSource(
        type="postgres",
        dsn="postgresql://user:pass@localhost:5432/testdb",
        operations={
            "get_item": PostgresOperation(
                query="SELECT id, name FROM items WHERE id = :id",
                params={"id": OperationParameter(type="string", required=True)},
                returns="item.json",
            )
        },
    )
    assert valid_dsn.type == "postgres"

    # Valid with database + host + port
    valid_fields = PostgresSource(
        type="postgres",
        database="testdb",
        host="localhost",
        port=5432,
        user="testuser",
        operations={
            "list_items": PostgresOperation(
                query="SELECT id, name FROM items",
                returns="items.json",
            )
        },
    )
    assert valid_fields.database == "testdb"

    # Invalid: neither dsn nor database
    with pytest.raises(ValueError, match="requires either 'dsn' or 'database'"):
        PostgresSource(
            type="postgres",
            host="localhost",
            operations={},
        )

    # Invalid port
    with pytest.raises(ValueError, match="port must be between 1 and 65535"):
        PostgresSource(
            type="postgres",
            database="testdb",
            port=99999,
            operations={},
        )

    # Empty query
    with pytest.raises(ValueError, match="query must not be empty"):
        PostgresOperation(query="   ", returns="item.json")

    # Read-only query starting with non-SELECT statement
    with pytest.raises(ValueError, match="Read-only Postgres query cannot start with 'DELETE'"):
        PostgresOperation(query="DELETE FROM items WHERE id = :id", returns="item.json")

    # Query with undeclared parameter
    with pytest.raises(ValueError, match="query parameter 'missing' is not declared"):
        PostgresOperation(
            query="SELECT id FROM items WHERE id = :missing",
            params={},
            returns="item.json",
        )


@pytest.fixture
def isolated_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, name TEXT, price INTEGER)")
    conn.execute("INSERT INTO items VALUES ('item_1', 'Widget', 100)")
    conn.execute("INSERT INTO items VALUES ('item_2', 'Gadget', 200)")
    conn.commit()
    yield conn
    conn.close()


def test_postgres_source_execute_object_query(isolated_db) -> None:
    cfg = PostgresSource(
        type="postgres",
        database="testdb",
        operations={
            "get_item": PostgresOperation(
                query="SELECT id, name, price FROM items WHERE id = :id",
                params={"id": OperationParameter(type="string", required=True)},
                returns="item.json",
            )
        },
    )
    schemas = {
        "item.json": {
            "type": "object",
            "required": ["id", "name", "price"],
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "price": {"type": "integer"},
            },
        }
    }
    source = PostgresOperationSource(cfg, schemas, connection=isolated_db)
    result = source.execute("get_item", {"id": "item_1"}, timeout=5.0)
    assert result == {"id": "item_1", "name": "Widget", "price": 100}


def test_postgres_source_execute_array_query(isolated_db) -> None:
    cfg = PostgresSource(
        type="postgres",
        database="testdb",
        operations={
            "list_items": PostgresOperation(
                query="SELECT id, name, price FROM items ORDER BY id",
                params={},
                returns="items_list.json",
            )
        },
    )
    schemas = {
        "items_list.json": {
            "type": "array",
            "items": {"type": "object", "required": ["id", "name"]},
        }
    }
    source = PostgresOperationSource(cfg, schemas, connection=isolated_db)
    result = source.execute("list_items", {}, timeout=5.0)
    assert len(result) == 2
    assert result[0]["id"] == "item_1"
    assert result[1]["id"] == "item_2"


def test_postgres_source_missing_required_param(isolated_db) -> None:
    cfg = PostgresSource(
        type="postgres",
        database="testdb",
        operations={
            "get_item": PostgresOperation(
                query="SELECT id FROM items WHERE id = :id",
                params={"id": OperationParameter(type="string", required=True)},
                returns="item.json",
            )
        },
    )
    source = PostgresOperationSource(cfg, {"item.json": {"type": "object"}}, connection=isolated_db)
    with pytest.raises(SourceError, match="missing required parameter: id"):
        source.execute("get_item", {}, timeout=5.0)


def test_postgres_source_unexpected_param(isolated_db) -> None:
    cfg = PostgresSource(
        type="postgres",
        database="testdb",
        operations={
            "get_item": PostgresOperation(
                query="SELECT id FROM items WHERE id = :id",
                params={"id": OperationParameter(type="string", required=True)},
                returns="item.json",
            )
        },
    )
    source = PostgresOperationSource(cfg, {"item.json": {"type": "object"}}, connection=isolated_db)
    with pytest.raises(SourceError, match="unexpected parameter: extra"):
        source.execute("get_item", {"id": "item_1", "extra": "val"}, timeout=5.0)


def test_postgres_source_unknown_operation(isolated_db) -> None:
    cfg = PostgresSource(type="postgres", database="testdb", operations={})
    source = PostgresOperationSource(cfg, {}, connection=isolated_db)
    with pytest.raises(SourceError, match="unknown operation 'nonexistent'"):
        source.execute("nonexistent", {}, timeout=5.0)


def test_postgres_source_schema_violation(isolated_db) -> None:
    cfg = PostgresSource(
        type="postgres",
        database="testdb",
        operations={
            "get_item": PostgresOperation(
                query="SELECT id, name FROM items WHERE id = :id",
                params={"id": OperationParameter(type="string", required=True)},
                returns="item.json",
            )
        },
    )
    # Schema requires price, which query doesn't select
    schemas = {
        "item.json": {
            "type": "object",
            "required": ["id", "price"],
            "properties": {"price": {"type": "integer"}},
        }
    }
    source = PostgresOperationSource(cfg, schemas, connection=isolated_db)
    with pytest.raises(SourceError, match="violates returns schema"):
        source.execute("get_item", {"id": "item_1"}, timeout=5.0)


def test_postgres_source_missing_returns_schema(isolated_db) -> None:
    cfg = PostgresSource(
        type="postgres",
        database="testdb",
        operations={
            "get_item": PostgresOperation(
                query="SELECT id FROM items WHERE id = :id",
                params={"id": OperationParameter(type="string", required=True)},
                returns="nonexistent.json",
            )
        },
    )
    source = PostgresOperationSource(cfg, {}, connection=isolated_db)
    with pytest.raises(SourceError, match="missing schema for operation returns"):
        source.execute("get_item", {"id": "item_1"}, timeout=5.0)


def test_postgres_source_driver_not_installed_error(monkeypatch) -> None:
    import sys

    # Simulate neither psycopg nor psycopg2 installed
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.setitem(sys.modules, "psycopg2", None)

    cfg = PostgresSource(
        type="postgres",
        dsn="postgresql://user:pass@localhost/db",
        operations={"test": PostgresOperation(query="SELECT 1", returns="out.json")},
    )
    source = PostgresOperationSource(cfg, {"out.json": {"type": "object"}})
    with pytest.raises(SourceError, match="PostgreSQL driver .* is not installed"):
        source.execute("test", {}, timeout=1.0)


def test_postgres_source_connect_with_psycopg_mock(monkeypatch) -> None:
    mock_psycopg = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.description = [("id",), ("val",)]
    mock_cursor.fetchall.return_value = [("1", "hello")]
    mock_conn.cursor.return_value = mock_cursor
    mock_psycopg.connect.return_value = mock_conn

    monkeypatch.setattr(
        "llmform.sources.postgres.PostgresOperationSource._get_connection",
        lambda self: mock_conn,
    )

    cfg = PostgresSource(
        type="postgres",
        database="testdb",
        operations={
            "query_val": PostgresOperation(
                query="SELECT id, val FROM tbl WHERE id = :id",
                params={"id": OperationParameter(type="string", required=True)},
                returns="out.json",
            )
        },
    )
    source = PostgresOperationSource(cfg, {"out.json": {"type": "object"}})
    res = source.execute("query_val", {"id": "1"}, timeout=2.0)
    assert res == {"id": "1", "val": "hello"}
    # Verify parameter formatting was converted for non-sqlite drivers
    mock_cursor.execute.assert_called_once_with(
        "SELECT id, val FROM tbl WHERE id = %(id)s", {"id": "1"}
    )
