"""Offline validation and failure-injection tests for the opt-in durable path."""
from contextlib import contextmanager
import traceback
from unittest.mock import MagicMock, Mock

import pandas as pd
import pytest

from api import db, durable
from api.pipeline import Evaluation, PipelineError


def frame(count=2):
    return pd.DataFrame([
        [f"role-{i}", None, None, "Remote", None, "Synthetic description",
         f"https://example.invalid/{i}"]
        for i in range(count)
    ], columns=db.VACANCY_COLUMNS, dtype=object)


@pytest.fixture
def no_database(monkeypatch):
    connect = Mock(side_effect=AssertionError("No connection expected"))
    monkeypatch.setattr(db, "_connect", connect)
    yield connect
    connect.assert_not_called()


@pytest.mark.parametrize("scope", ["", " ", None, 1, "x" * 129, "\u044f" * 65, "bad\x00scope", "\ud800"])
def test_invalid_scope_has_no_side_effects(scope, no_database):
    with pytest.raises(ValueError):
        durable.process_durable_batch(frame(), scope=scope, evaluate=Mock())
    with pytest.raises(ValueError):
        durable.dispatch_outbox(scope=scope, notify=Mock())


@pytest.mark.parametrize("limit", [0, -1, True, None, 1.5, 1001])
def test_invalid_dispatch_limit(limit, no_database):
    with pytest.raises(ValueError):
        durable.dispatch_outbox(scope="test:v1", notify=Mock(), limit=limit)


def test_invalid_batch_has_no_side_effects(no_database):
    with pytest.raises(ValueError):
        durable.process_durable_batch(frame(), scope="test:v1", evaluate=Mock(), max_rows=1)
    with pytest.raises(ValueError):
        durable.process_durable_batch(frame().drop(columns=db.VACANCY_COLUMNS[0]),
                                      scope="test:v1", evaluate=Mock())


def test_empty_batch_never_connects_or_evaluates(no_database):
    evaluate = Mock()
    report = durable.process_durable_batch(frame(0), scope="test:v1", evaluate=evaluate)
    assert report == durable.DurableReport(0, 0, 0, 0, 0, 0)
    evaluate.assert_not_called()


def test_keys_are_deterministic_and_normalize_missing_values():
    row = frame(1).iloc[0].tolist()
    first, payload = durable._key(row)
    row[1] = pd.NA
    row[2] = float("nan")
    second, _ = durable._key(row)
    assert first == second and len(first) == 64
    assert payload[1:3] == [None, None]
    row[5] = "Changed description"
    assert durable._key(row)[0] != first


@pytest.mark.parametrize("options", [
    {"job_key": "invalid"}, {"expected_attempts": 0}, {"expected_attempts": True},
    {"decision": "inflight"}, {"decision": "pending"},
    {"decision": "pending", "acknowledge_duplicate_risk": 1},
])
def test_invalid_resolution_never_connects(options, no_database):
    arguments = dict(scope="test:v1", job_key="a" * 64, expected_attempts=1, decision="sent")
    arguments.update(options)
    with pytest.raises(ValueError):
        durable.resolve_notification(**arguments)


@pytest.mark.parametrize("seconds", [0, -1, True, 1.5, 31536001])
def test_invalid_recovery_threshold(seconds, no_database):
    with pytest.raises(ValueError):
        durable.recover_stale_claims(scope="test:v1", older_than_seconds=seconds)


def test_invalid_inspection_never_connects(no_database):
    with pytest.raises(ValueError):
        durable.list_notifications(scope="test:v1", state="all")
    with pytest.raises(ValueError):
        durable.list_notifications(scope="test:v1", limit=0)
    with pytest.raises(ValueError):
        durable.dispatch_outbox(scope="test:v1", notify=None)


def test_async_callbacks_are_rejected_before_connecting(no_database):
    async def asynchronous(*args):
        return None
    with pytest.raises(ValueError, match="synchronous"):
        durable.dispatch_outbox(scope="test:v1", notify=asynchronous)
    with pytest.raises(ValueError, match="synchronous"):
        durable.process_durable_batch(frame(), scope="test:v1", evaluate=asynchronous)


def test_wrapped_coroutine_is_uncertain_not_success(monkeypatch):
    async def unfinished():
        raise AssertionError("This coroutine must not run")
    monkeypatch.setattr(durable, "_claim", Mock(side_effect=[("a" * 64, "message", "token"), None]))
    acknowledge = Mock()
    monkeypatch.setattr(durable, "_acknowledge", acknowledge)
    report = durable.dispatch_outbox(scope="test:v1", notify=lambda message: unfinished())
    assert report == durable.DispatchReport(0, 1)
    acknowledge.assert_called_once_with("test:v1", "a" * 64, "token", "uncertain")


def test_transaction_closes_and_sanitizes_db_failure(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = RuntimeError("synthetic-private-detail")
    monkeypatch.setattr(db, "_connect", Mock(return_value=connection))
    with pytest.raises(PipelineError) as caught:
        durable.initialize_store()
    assert caught.value.stage == "store_initialization"
    assert "synthetic-private-detail" not in "".join(traceback.format_exception(caught.value))
    connection.close.assert_called_once()
    assert connection.__exit__.call_args.args[0] is RuntimeError


def test_claim_commit_failure_never_sends(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("a" * 64, "Synthetic message")
    connection.__exit__.side_effect = RuntimeError("synthetic-commit-uncertain")
    monkeypatch.setattr(db, "_connect", Mock(return_value=connection))
    notify = Mock()
    with pytest.raises(PipelineError, match="notification_claim"):
        durable.dispatch_outbox(scope="test:v1", notify=notify)
    notify.assert_not_called()
    connection.close.assert_called_once()


def test_ack_failure_does_not_retry_delivery(monkeypatch):
    claim = Mock(return_value=("a" * 64, "Synthetic message", "synthetic-token"))
    acknowledge = Mock(side_effect=PipelineError("notification_acknowledgement"))
    monkeypatch.setattr(durable, "_claim", claim)
    monkeypatch.setattr(durable, "_acknowledge", acknowledge)
    notify = Mock()
    with pytest.raises(PipelineError):
        durable.dispatch_outbox(scope="test:v1", notify=notify)
    notify.assert_called_once_with("Synthetic message")
    claim.assert_called_once()
    acknowledge.assert_called_once()


def test_interruption_leaves_claim_unacknowledged(monkeypatch):
    monkeypatch.setattr(durable, "_claim", Mock(return_value=("a" * 64, "message", "token")))
    acknowledge = Mock()
    monkeypatch.setattr(durable, "_acknowledge", acknowledge)
    with pytest.raises(KeyboardInterrupt):
        durable.dispatch_outbox(scope="test:v1", notify=Mock(side_effect=KeyboardInterrupt))
    acknowledge.assert_not_called()


def test_uncertain_outcome_is_acknowledged_without_payload_logging(monkeypatch, capsys):
    monkeypatch.setattr(durable, "_claim", Mock(side_effect=[
        ("a" * 64, "message", "token"), None]))
    acknowledge = Mock()
    monkeypatch.setattr(durable, "_acknowledge", acknowledge)
    report = durable.dispatch_outbox(scope="test:v1",
                                     notify=Mock(side_effect=TimeoutError("synthetic-private-detail")))
    assert report == durable.DispatchReport(0, 1)
    acknowledge.assert_called_once_with("test:v1", "a" * 64, "token", "uncertain")
    assert capsys.readouterr() == ("", "")


def test_evaluation_failure_has_no_commit_transaction(monkeypatch):
    cursor = Mock()
    cursor.fetchall.return_value = []
    stages = []
    @contextmanager
    def transaction(stage):
        stages.append(stage)
        yield cursor
    monkeypatch.setattr(durable, "_transaction", transaction)
    evaluate = Mock(side_effect=[Evaluation("Synthetic", 1), RuntimeError("synthetic-private-detail")])
    with pytest.raises(PipelineError, match="evaluation") as caught:
        durable.process_durable_batch(frame(), scope="test:v1", evaluate=evaluate)
    assert caught.value.position == 1 and stages == ["deduplication"]


def test_all_existing_jobs_skip_evaluator(monkeypatch):
    source = frame()
    keys = [durable._key(row)[0] for row in source.itertuples(index=False, name=None)]
    cursor = Mock()
    cursor.fetchall.return_value = [(key,) for key in keys]
    @contextmanager
    def transaction(stage):
        assert stage == "deduplication"
        yield cursor
    monkeypatch.setattr(durable, "_transaction", transaction)
    evaluate = Mock()
    report = durable.process_durable_batch(source, scope="test:v1", evaluate=evaluate)
    assert report == durable.DurableReport(2, 0, 2, 0, 0, 0)
    evaluate.assert_not_called()
