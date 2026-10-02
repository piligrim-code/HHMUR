"""Shared owned disposable PostgreSQL fixtures; never use deployment data."""
from contextlib import closing
import os
import re
from types import SimpleNamespace
import uuid

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn
import pytest

from api import db


@pytest.fixture(scope="session")
def postgres():
    name = os.environ["HHMUR_TEST_DB"]
    if not re.fullmatch(r"hhmur_probe_[0-9a-f]{12}", name):
        raise ValueError("Refusing non-test database")
    common = dict(host="127.0.0.1", port=int(os.environ["HHMUR_TEST_PORT"]), dbname=name,
                  password=os.environ["HHMUR_TEST_PASSWORD"], connect_timeout=5, sslmode="disable")
    admin = psycopg2.connect(user="hhmur_probe", **common)
    admin.autocommit = True
    role = "app_" + uuid.uuid4().hex[:16]
    try:
        with admin.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                           "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'")
            if cursor.fetchone()[0]:
                raise ValueError("Refusing a nonempty test database")
            cursor.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s NOSUPERUSER NOCREATEDB NOCREATEROLE").format(sql.Identifier(role)),
                           (common["password"],))
        yield admin, dict(common, user=role)
    finally:
        admin.close()


@pytest.fixture
def database(postgres, monkeypatch):
    admin, params = postgres
    schema = "probe_" + uuid.uuid4().hex[:16]
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(sql.Identifier(schema), sql.Identifier(params["user"])))
    dsn = make_dsn(**params, options="-csearch_path=" + schema + " -cstatement_timeout=3000 -clock_timeout=2000")
    monkeypatch.setenv("HHMUR_DATABASE_URL", dsn)
    opened = []
    original = db._connect
    def tracked():
        connection = original()
        opened.append(connection)
        return connection
    monkeypatch.setattr(db, "_connect", tracked)

    def execute(query, values=None, fetch=False):
        with closing(psycopg2.connect(dsn)) as connection:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, values)
                    return cursor.fetchall() if fetch else None
    try:
        yield SimpleNamespace(execute=execute, schema=schema, opened=opened)
    finally:
        leaked = [connection for connection in opened if not connection.closed]
        for connection in leaked:
            connection.close()
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        assert not leaked, "Adapter leaked a connection"
