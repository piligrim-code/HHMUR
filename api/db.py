"""DataFrame persistence with explicit, environment-only configuration."""
from contextlib import closing
import os

import pandas as pd
import psycopg2
from psycopg2 import sql

VACANCY_COLUMNS = (
    "Название_вакансии", "Работодатель", "Опыт_работы", "Город",
    "Требования", "Описание_работы", "Ссылка",
)
READY_COLUMNS = VACANCY_COLUMNS + ("Ответ", "Оценка")
COLUMN_ALIASES = {
    "Название вакансии": "Название_вакансии", "Опыт работы": "Опыт_работы",
    "Описание работы": "Описание_работы", "response_text": "Ответ",
    "evaluation_score": "Оценка",
}


def _connect():
    dsn = os.environ.get("HHMUR_DATABASE_URL", "").strip()
    if not dsn:
        raise ValueError("Set HHMUR_DATABASE_URL before accessing the database")
    return psycopg2.connect(dsn, connect_timeout=5)


def _write_dataframe(df, table_name, columns):
    frame = df.rename(columns=COLUMN_ALIASES)
    if frame.columns.duplicated().any():
        raise ValueError("Duplicate DataFrame columns after normalization")
    if not set(columns).issubset(frame.columns):
        raise ValueError("DataFrame is missing required vacancy columns")
    # Column order must not depend on the caller's DataFrame layout.
    frame = frame.loc[:, list(columns)].astype(object)
    frame = frame.where(pd.notna(frame), None)
    rows = list(frame.itertuples(index=False, name=None))
    table = sql.Identifier(table_name)
    definitions = sql.SQL(", ").join(
        sql.SQL("{} TEXT").format(sql.Identifier(name)) for name in columns
    )
    create = sql.SQL("CREATE TABLE IF NOT EXISTS {} (id SERIAL PRIMARY KEY, {})").format(table, definitions)
    insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        table, sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    with closing(_connect()) as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(create)
                if rows:
                    cur.executemany(insert, rows)


def import_dataframe_to_postgresql(df, table_name="vacancies"):
    _write_dataframe(df, table_name, VACANCY_COLUMNS)


def import_dataframe_to_postgresql_ready(df, table_name="vacancies_ready"):
    _write_dataframe(df, table_name, READY_COLUMNS)


def export_dataframe_from_postgresql(table_name="vacancies", chunksize=1000):
    if isinstance(chunksize, bool) or not isinstance(chunksize, int) or chunksize <= 0:
        raise ValueError("chunksize must be a positive integer")
    query = sql.SQL("SELECT * FROM {}").format(sql.Identifier(table_name))
    with closing(_connect()) as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(query)
                columns = [field[0] for field in cur.description]
                rows = []
                while True:
                    chunk = cur.fetchmany(chunksize)
                    if not chunk:
                        break
                    rows.extend(chunk)
    return pd.DataFrame.from_records(rows, columns=columns)
