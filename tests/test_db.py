import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from psycopg2 import sql

from api import db


@pytest.fixture
def connection(monkeypatch):
    monkeypatch.setenv("HHMUR_DATABASE_URL", "postgresql://localhost/test_fixture")
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = conn.cursor.return_value.__enter__.return_value
    with patch.object(db.psycopg2, "connect", return_value=conn) as connect:
        yield conn, cur, connect


def frame(ready=False):
    columns = db.READY_COLUMNS if ready else db.VACANCY_COLUMNS
    return pd.DataFrame([{name: str(i) for i, name in enumerate(columns)}])


def test_missing_configuration_never_connects(monkeypatch):
    monkeypatch.delenv("HHMUR_DATABASE_URL", raising=False)
    with patch.object(db.psycopg2, "connect") as connect:
        with pytest.raises(ValueError, match="HHMUR_DATABASE_URL"):
            db.export_dataframe_from_postgresql()
        connect.assert_not_called()


def test_connection_uses_only_explicit_environment(connection):
    conn, cur, connect = connection
    cur.description = [("id",)]
    cur.fetchmany.return_value = []
    db.export_dataframe_from_postgresql()
    connect.assert_called_once_with("postgresql://localhost/test_fixture", connect_timeout=5)
    conn.close.assert_called_once()


def test_quoted_identifier_and_empty_schema(connection):
    _, cur, _ = connection
    cur.description = [("id",), ("value",)]
    cur.fetchmany.return_value = []
    table = 'vacancies; DROP TABLE anything; --'
    result = db.export_dataframe_from_postgresql(table)
    cur.execute.assert_called_once_with(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
    assert list(result.columns) == ["id", "value"]
    assert result.empty


def test_write_reorders_fields_without_printing_records(connection, capsys):
    conn, cur, _ = connection
    source = frame().loc[:, list(reversed(db.VACANCY_COLUMNS))]
    db.import_dataframe_to_postgresql(source)
    assert cur.executemany.call_args.args[1] == [tuple(str(i) for i in range(7))]
    assert capsys.readouterr().out == ""
    conn.close.assert_called_once()


def test_ready_table_and_aliases(connection):
    _, cur, _ = connection
    source = frame(True).rename(columns={"Ответ": "response_text", "Оценка": "evaluation_score"})
    db.import_dataframe_to_postgresql_ready(source)
    assert cur.executemany.call_args.args[0].seq[1] == sql.Identifier("vacancies_ready")
    assert len(cur.executemany.call_args.args[1][0]) == 9


def test_failure_closes_and_reaches_rollback_context(connection):
    conn, cur, _ = connection
    cur.executemany.side_effect = db.psycopg2.DatabaseError("synthetic failure")
    with pytest.raises(db.psycopg2.DatabaseError):
        db.import_dataframe_to_postgresql(frame())
    assert conn.__exit__.call_args.args[0] is db.psycopg2.DatabaseError
    conn.close.assert_called_once()


def test_connect_failure_not_masked(monkeypatch):
    monkeypatch.setenv("HHMUR_DATABASE_URL", "postgresql://localhost/test_fixture")
    with patch.object(db.psycopg2, "connect", side_effect=db.psycopg2.OperationalError("offline")):
        with pytest.raises(db.psycopg2.OperationalError, match="offline"):
            db.export_dataframe_from_postgresql()


@pytest.mark.parametrize("chunksize", [0, -1, True, 1.5])
def test_invalid_chunksize_never_connects(chunksize):
    with patch.object(db, "_connect") as connect:
        with pytest.raises(ValueError):
            db.export_dataframe_from_postgresql(chunksize=chunksize)
        connect.assert_not_called()


def test_missing_columns_never_connects():
    with patch.object(db, "_connect") as connect:
        with pytest.raises(ValueError):
            db.import_dataframe_to_postgresql(pd.DataFrame({"other": [1]}))
        connect.assert_not_called()


def test_sources_compile_without_importing_integrations():
    root = Path(__file__).resolve().parents[1]
    for path in list(root.glob("*.py")) + list((root / "api").glob("*.py")):
        ast.parse(path.read_text(encoding="utf-8"), filename=path.name)


def test_db_has_no_connection_string_literal():
    tree = ast.parse(Path(db.__file__).read_text(encoding="utf-8"))
    values = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not any("postgres://" in v or "postgresql://" in v for v in values)


def invoke(operation, table):
    if operation == "read":
        return db.export_dataframe_from_postgresql(table)
    writer = db.import_dataframe_to_postgresql_ready if operation == "ready" else db.import_dataframe_to_postgresql
    return writer(frame(ready=operation == "ready"), table)


@pytest.mark.parametrize("operation", ["write", "ready", "read"])
@pytest.mark.parametrize("table", [None, 12, "", "bad\x00name", "x" * 64, "\u044f" * 32, "\ud800"])
def test_invalid_identifier_never_connects(operation, table):
    with patch.object(db, "_connect") as connect:
        with pytest.raises(ValueError, match="table_name"):
            invoke(operation, table)
        connect.assert_not_called()


@pytest.mark.parametrize("operation", ["write", "ready", "read"])
@pytest.mark.parametrize("table", ["x" * 63, "\u044f" * 31 + "x"])
def test_identifier_limit_counts_utf8_bytes_and_allows_exact_boundary(connection, operation, table):
    _, cursor, connect = connection
    cursor.description = [("id",)]
    cursor.fetchmany.return_value = []
    invoke(operation, table)
    connect.assert_called_once()


def test_alias_collision_is_rejected_before_connecting():
    source = frame()
    alias, normalized = next(iter(db.COLUMN_ALIASES.items()))
    source[alias] = source[normalized]
    with patch.object(db, "_connect") as connect:
        with pytest.raises(ValueError, match="Duplicate"):
            db.import_dataframe_to_postgresql(source)
        connect.assert_not_called()
