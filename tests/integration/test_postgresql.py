"""Real adapter checks; use the owned Docker runner, never a deployment DSN."""
import os

import pandas as pd
import psycopg2
from psycopg2 import sql
import pytest

from api import db
from api.main import run_database_pipeline
from api.pipeline import Evaluation, PipelineError

pytestmark = pytest.mark.skipif(os.environ.get("HHMUR_INTEGRATION") != "1", reason="requires owned disposable PostgreSQL")


def frame(count=3, ready=False):
    columns = db.READY_COLUMNS if ready else db.VACANCY_COLUMNS
    return pd.DataFrame([{name: f"synthetic-{index}-{column}" for column, name in enumerate(columns)} for index in range(count)], columns=columns)


def test_normal_schema_append_export_and_multiple_fetch_batches(database):
    source = frame(5)
    db.import_dataframe_to_postgresql(source.iloc[:3])
    db.import_dataframe_to_postgresql(source.iloc[3:].loc[:, list(reversed(db.VACANCY_COLUMNS))])
    result = db.export_dataframe_from_postgresql(chunksize=2).sort_values("id").reset_index(drop=True)
    assert result["id"].tolist() == [1, 2, 3, 4, 5]
    pd.testing.assert_frame_equal(result[list(db.VACANCY_COLUMNS)], source)
    fields = database.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                              (database.schema, "vacancies"), fetch=True)
    assert fields == [("id", "integer")] + [(column, "text") for column in db.VACANCY_COLUMNS]


def test_evaluated_aliases_numeric_zero_and_nulls(database):
    source = frame(2, ready=True)
    source = source.rename(columns={target: alias for alias, target in db.COLUMN_ALIASES.items()})
    source["response_text"] = ["Synthetic reply with apostrophe ' and newline\n", None]
    source["evaluation_score"] = [0, 1]
    source[db.VACANCY_COLUMNS[1]] = [pd.NA, None]
    db.import_dataframe_to_postgresql_ready(source)
    result = db.export_dataframe_from_postgresql("vacancies_ready").sort_values("id")
    assert list(result.columns) == ["id", *db.READY_COLUMNS]
    assert result[db.READY_COLUMNS[-1]].tolist() == ["0", "1"]
    assert result[db.READY_COLUMNS[-2]].iloc[0] == source["response_text"].iloc[0]
    assert pd.isna(result[db.READY_COLUMNS[-2]].iloc[1])
    assert result[db.VACANCY_COLUMNS[1]].isna().all()


@pytest.mark.parametrize("ready", [False, True])
def test_empty_frame_creates_empty_declared_schema(database, ready):
    columns = db.READY_COLUMNS if ready else db.VACANCY_COLUMNS
    writer = db.import_dataframe_to_postgresql_ready if ready else db.import_dataframe_to_postgresql
    writer(pd.DataFrame(columns=columns), "empty")
    result = db.export_dataframe_from_postgresql("empty", chunksize=1)
    assert result.empty and list(result.columns) == ["id", *columns]


def test_table_and_value_quotes_do_not_execute_sql(database):
    database.execute("CREATE TABLE sentinel (value INTEGER)")
    database.execute("INSERT INTO sentinel VALUES (42)")
    table = 'sample"; DROP TABLE sentinel; --'
    source = frame(1)
    source.iloc[0, 0] = "synthetic'); DROP TABLE sentinel; --"
    db.import_dataframe_to_postgresql(source, table)
    result = db.export_dataframe_from_postgresql(table)
    assert result.iloc[0, 1] == source.iloc[0, 0]
    assert database.execute("SELECT value FROM sentinel", fetch=True) == [(42,)]


def test_batch_failure_rolls_back_every_new_row_and_next_call_succeeds(database):
    initial = frame(1)
    db.import_dataframe_to_postgresql(initial)
    database.execute(sql.SQL("ALTER TABLE vacancies ADD CONSTRAINT reject_synthetic CHECK ({} <> 'reject-row')").format(sql.Identifier(db.VACANCY_COLUMNS[0])))
    incoming = frame(2)
    incoming.iloc[1, 0] = "reject-row"
    with pytest.raises(psycopg2.errors.CheckViolation):
        db.import_dataframe_to_postgresql(incoming)
    result = db.export_dataframe_from_postgresql()
    assert len(result) == 1
    assert result.iloc[0, 1] == initial.iloc[0, 0]
    db.import_dataframe_to_postgresql(frame(1))
    assert len(db.export_dataframe_from_postgresql()) == 2


def test_new_table_creation_is_rolled_back_when_payload_cannot_adapt(database):
    source = frame(2).astype(object)
    source.iloc[1, 0] = object()
    with pytest.raises(psycopg2.ProgrammingError):
        db.import_dataframe_to_postgresql(source, "rolled_back")
    assert database.execute("SELECT to_regclass('rolled_back')", fetch=True) == [(None,)]


def test_existing_incompatible_table_is_not_silently_migrated(database):
    database.execute("CREATE TABLE legacy (legacy_value TEXT)")
    database.execute("INSERT INTO legacy VALUES ('synthetic original')")
    with pytest.raises(psycopg2.errors.UndefinedColumn):
        db.import_dataframe_to_postgresql(frame(1), "legacy")
    assert database.execute("SELECT * FROM legacy", fetch=True) == [("synthetic original",)]


def test_missing_table_export_closes_connection(database):
    with pytest.raises(psycopg2.errors.UndefinedTable):
        db.export_dataframe_from_postgresql("not_present")
    assert database.opened and database.opened[-1].closed


def test_adapter_works_without_superuser_or_role_creation_permissions(database):
    assert database.execute("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user", fetch=True) == [(False, False, False)]
    db.import_dataframe_to_postgresql(frame(1))
    assert len(db.export_dataframe_from_postgresql()) == 1


def test_readonly_transaction_rejects_write_without_partial_table(database, monkeypatch):
    original = db._connect
    def readonly():
        connection = original()
        connection.set_session(readonly=True)
        return connection
    monkeypatch.setattr(db, "_connect", readonly)
    with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
        db.import_dataframe_to_postgresql(frame(1), "read_only")
    assert database.execute("SELECT to_regclass('read_only')", fetch=True) == [(None,)]


@pytest.mark.parametrize("short", ["x" * 63, "\u044f" * 31 + "x"])
def test_overlong_identifier_does_not_alias_an_existing_table(database, short):
    db.import_dataframe_to_postgresql(frame(1), short)
    try:
        db.import_dataframe_to_postgresql(frame(1), short + "extra")
    except ValueError as error:
        assert "63" in str(error)
    else:
        assert len(db.export_dataframe_from_postgresql(short)) == 1, "Overlong identifier silently appended to an existing table"
        pytest.fail("Overlong identifiers must be rejected")
    assert len(db.export_dataframe_from_postgresql(short)) == 1


def test_pipeline_commits_before_notifications_on_separate_connection(database):
    source = frame(3)
    db.import_dataframe_to_postgresql(source)
    messages = []
    def evaluate(title, description):
        return Evaluation("Synthetic reply: " + title, int(title != source.iloc[1, 0]))
    def notify(message):
        # A new connection must see the entire committed batch.
        saved = db.export_dataframe_from_postgresql("vacancies_ready").sort_values("id")
        assert saved[db.READY_COLUMNS[-1]].tolist() == ["1", "0", "1"]
        assert saved[db.READY_COLUMNS[-2]].tolist() == [
            "Synthetic reply: " + title for title in source.iloc[:, 0]]
        messages.append(message)
    report = run_database_pipeline(evaluate=evaluate, notify=notify)
    assert report.persisted == 3 and report.notifications_sent == 2
    assert report.notification_failures == () and len(messages) == 2
    assert len(db.export_dataframe_from_postgresql()) == 3


def test_pipeline_evaluation_failure_leaves_no_output_table(database):
    db.import_dataframe_to_postgresql(frame(2))
    evaluated, messages = [], []
    def evaluate(title, description):
        evaluated.append(title)
        if len(evaluated) == 2:
            raise RuntimeError("Synthetic provider failure")
        return Evaluation("Synthetic reply", 1)
    with pytest.raises(PipelineError, match="evaluation"):
        run_database_pipeline(evaluate=evaluate, notify=messages.append)
    assert database.execute("SELECT to_regclass('vacancies_ready')", fetch=True) == [(None,)]
    assert messages == [] and len(evaluated) == 2


def test_pipeline_transaction_failure_rolls_back_and_suppresses_notifications(database):
    source = frame(2)
    db.import_dataframe_to_postgresql(source)
    db.import_dataframe_to_postgresql_ready(pd.DataFrame(columns=db.READY_COLUMNS))
    database.execute(sql.SQL("ALTER TABLE vacancies_ready ADD CONSTRAINT reject_pipeline CHECK ({} <> %s)").format(
        sql.Identifier(db.VACANCY_COLUMNS[0])), (source.iloc[1, 0],))
    messages = []
    with pytest.raises(PipelineError, match="persistence"):
        run_database_pipeline(evaluate=lambda title, description: Evaluation("Synthetic reply", 1),
                              notify=messages.append)
    assert db.export_dataframe_from_postgresql("vacancies_ready").empty
    assert len(db.export_dataframe_from_postgresql()) == 2 and messages == []


def test_pipeline_notification_failure_keeps_committed_results(database):
    db.import_dataframe_to_postgresql(frame(2))
    attempts = []
    def notify(message):
        attempts.append(message)
        raise TimeoutError("Synthetic ambiguous delivery")
    report = run_database_pipeline(evaluate=lambda title, description: Evaluation("Synthetic reply", 1),
                                   notify=notify)
    assert report.notification_failures == (0, 1)
    assert report.persisted == 2 and report.notifications_sent == 0
    assert len(attempts) == 2
    assert len(db.export_dataframe_from_postgresql("vacancies_ready")) == 2


def test_empty_database_pipeline_makes_no_evaluation_or_output(database):
    db.import_dataframe_to_postgresql(frame(0))
    def forbidden(*args):
        pytest.fail("Empty input must not call an external adapter")
    report = run_database_pipeline(evaluate=forbidden, notify=forbidden)
    assert report.persisted == 0
    assert database.execute("SELECT to_regclass('vacancies_ready')", fetch=True) == [(None,)]
