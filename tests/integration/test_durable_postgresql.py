"""Durable state, rollback and concurrency checks against owned PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
from unittest.mock import Mock

import pandas as pd
import pytest

from api import db, durable
from api.pipeline import Evaluation, PipelineError

pytestmark = pytest.mark.skipif(os.environ.get("HHMUR_INTEGRATION") != "1",
                               reason="requires owned disposable PostgreSQL")
SCOPE = "synthetic-policy:v1:audience-A"


def frame(count=2):
    return pd.DataFrame([
        [f"role-{i}", None, None, "Remote", None, "Synthetic description",
         f"https://example.invalid/{i}"]
        for i in range(count)
    ], columns=db.VACANCY_COLUMNS, dtype=object)


@pytest.fixture
def store(database):
    durable.initialize_store()
    return database


def enqueue(source=None, scope=SCOPE, evaluate=None):
    return durable.process_durable_batch(
        frame() if source is None else source, scope=scope,
        evaluate=evaluate or (lambda title, description: Evaluation("Synthetic " + title, 1)))


def counts(store):
    return store.execute("""
        SELECT (SELECT count(*) FROM hhmur_results_v1),
               (SELECT count(*) FROM hhmur_outbox_v1)
    """, fetch=True)[0]


def expire_claims(store):
    store.execute("UPDATE hhmur_outbox_v1 SET started_at = clock_timestamp() - INTERVAL '2 hours' "
                  "WHERE state = 'inflight'")


def test_initialization_is_repeatable_and_does_not_touch_legacy_rows(database):
    db.import_dataframe_to_postgresql(frame(1))
    durable.initialize_store()
    durable.initialize_store()
    assert len(db.export_dataframe_from_postgresql()) == 1
    assert counts(database) == (0, 0)


def test_initialize_rolls_back_on_incompatible_existing_schema(database):
    database.execute("CREATE TABLE hhmur_outbox_v1 (legacy TEXT)")
    with pytest.raises(PipelineError, match="store_initialization"):
        durable.initialize_store()
    assert database.execute("SELECT to_regclass('hhmur_results_v1')", fetch=True) == [(None,)]
    assert database.execute("SELECT count(*) FROM hhmur_outbox_v1", fetch=True) == [(0,)]


def test_repeat_input_and_duplicate_indices_do_not_repeat_evaluation_or_delivery(store):
    source = pd.concat([frame(), frame().iloc[:1]])
    source.index = [99, 7, 99]
    evaluate = Mock(side_effect=lambda title, description: Evaluation("Synthetic " + title, 1))
    first = enqueue(source, evaluate=evaluate)
    assert first == durable.DurableReport(3, 1, 0, 0, 2, 2)
    messages = []
    def notify(message):
        assert counts(store) == (2, 2)
        messages.append(message)
    assert durable.dispatch_outbox(scope=SCOPE, notify=notify) == durable.DispatchReport(2, 0)
    second = enqueue(source.iloc[::-1], evaluate=evaluate)
    assert second == durable.DurableReport(3, 1, 2, 0, 0, 0)
    assert evaluate.call_count == 2
    assert durable.dispatch_outbox(scope=SCOPE, notify=notify) == durable.DispatchReport(0, 0)
    assert len(messages) == 2 and counts(store) == (2, 2)
    assert len(durable.list_notifications(scope=SCOPE, state="sent")) == 2


def test_aliases_and_missing_values_share_identity(store):
    source = frame(1)
    enqueue(source)
    source = source.rename(columns={value: key for key, value in db.COLUMN_ALIASES.items()})
    source.iat[0, 1] = pd.NA
    source.iat[0, 2] = float("nan")
    source["id"] = 123
    evaluate = Mock()
    report = enqueue(source, evaluate=evaluate)
    evaluate.assert_not_called()
    assert report.already_committed == 1 and counts(store) == (1, 1)


def test_scope_and_content_revision_are_explicit_new_work(store):
    source = frame(1)
    enqueue(source)
    enqueue(source, scope="synthetic-policy:v2:audience-B")
    source.iat[0, 5] = "Changed synthetic description"
    enqueue(source)
    assert counts(store) == (3, 3)
    assert durable.dispatch_outbox(scope=SCOPE, notify=Mock()).sent == 2
    assert len(durable.list_notifications(scope="synthetic-policy:v2:audience-B", state="pending")) == 1


def test_rejected_rows_are_deduplicated_without_outbox_entries(store):
    evaluate = Mock(return_value=Evaluation("Synthetic rejection", 0))
    assert enqueue(evaluate=evaluate).queued == 0
    assert enqueue(evaluate=evaluate).already_committed == 2
    assert evaluate.call_count == 2 and counts(store) == (2, 0)


def test_evaluation_error_leaves_no_partial_results(store):
    evaluate = Mock(side_effect=[Evaluation("Synthetic", 1), RuntimeError("Synthetic failure")])
    with pytest.raises(PipelineError, match="evaluation"):
        enqueue(evaluate=evaluate)
    assert counts(store) == (0, 0)


def test_outbox_failure_rolls_back_entire_batch_and_can_be_retried(store):
    source = frame()
    keys = [durable._key(row)[0] for row in source.itertuples(index=False, name=None)]
    store.execute("ALTER TABLE hhmur_outbox_v1 ADD CONSTRAINT reject_last CHECK (job_key <> %s)",
                  (max(keys),))
    with pytest.raises(PipelineError, match="durable_commit"):
        enqueue(source)
    assert counts(store) == (0, 0)
    store.execute("ALTER TABLE hhmur_outbox_v1 DROP CONSTRAINT reject_last")
    assert enqueue(source).committed == 2 and counts(store) == (2, 2)


def test_two_concurrent_evaluators_commit_only_one_result_and_message(store):
    barrier = Barrier(2)
    def evaluate(title, description):
        barrier.wait(timeout=10)
        return Evaluation("Synthetic concurrent result", 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(enqueue, frame(1), evaluate=evaluate) for _ in range(2)]
        reports = [future.result(timeout=20) for future in futures]
    assert sum(report.committed for report in reports) == 1
    assert sum(report.concurrent_skips for report in reports) == 1
    assert sum(report.queued for report in reports) == 1
    assert counts(store) == (1, 1)


def test_two_dispatchers_claim_distinct_messages(store):
    enqueue()
    barrier = Barrier(2)
    messages = []
    def notify(message):
        barrier.wait(timeout=10)
        messages.append(message)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(durable.dispatch_outbox, scope=SCOPE, notify=notify, limit=1)
                   for _ in range(2)]
        reports = [future.result(timeout=20) for future in futures]
    assert sum(report.sent for report in reports) == 2
    assert len(set(messages)) == 2
    assert len(durable.list_notifications(scope=SCOPE, state="sent")) == 2


def test_delivery_error_is_uncertain_and_not_automatically_retried(store):
    enqueue(frame(1))
    notify = Mock(side_effect=TimeoutError("Synthetic provider uncertainty"))
    assert durable.dispatch_outbox(scope=SCOPE, notify=notify) == durable.DispatchReport(0, 1)
    assert durable.dispatch_outbox(scope=SCOPE, notify=notify) == durable.DispatchReport(0, 0)
    notify.assert_called_once()
    event = durable.list_notifications(scope=SCOPE)[0]
    assert event["attempts"] == 1 and event["reason"] == "notification_error"
    assert "message" not in event and "response" not in event and counts(store) == (1, 1)


def test_interrupted_dispatch_persists_claim_and_recovery_only_quarantines(store):
    enqueue(frame(1))
    with pytest.raises(KeyboardInterrupt):
        durable.dispatch_outbox(scope=SCOPE, notify=Mock(side_effect=KeyboardInterrupt))
    assert len(durable.list_notifications(scope=SCOPE, state="inflight")) == 1
    assert durable.recover_stale_claims(scope=SCOPE) == 0
    expire_claims(store)
    assert durable.recover_stale_claims(scope=SCOPE) == 1
    assert durable.recover_stale_claims(scope=SCOPE) == 0
    event = durable.list_notifications(scope=SCOPE)[0]
    assert event["reason"] == "stale_claim"
    notify = Mock()
    assert durable.dispatch_outbox(scope=SCOPE, notify=notify).sent == 0
    notify.assert_not_called()


@pytest.mark.parametrize("decision", ["pending", "sent", "abandoned"])
def test_operator_resolution_requires_current_attempt_and_explicit_retry_consent(store, decision):
    enqueue(frame(1))
    durable.dispatch_outbox(scope=SCOPE, notify=Mock(side_effect=TimeoutError))
    event = durable.list_notifications(scope=SCOPE)[0]
    options = dict(scope=SCOPE, job_key=event["job_key"], expected_attempts=event["attempts"],
                   decision=decision)
    if decision == "pending":
        with pytest.raises(ValueError, match="acknowledge_duplicate_risk"):
            durable.resolve_notification(**options)
        options["acknowledge_duplicate_risk"] = True
    durable.resolve_notification(**options)
    with pytest.raises(durable.StateConflict):
        durable.resolve_notification(**options)
    notify = Mock()
    report = durable.dispatch_outbox(scope=SCOPE, notify=notify)
    assert report.sent == int(decision == "pending")
    if decision == "pending":
        assert durable.list_notifications(scope=SCOPE, state="sent")[0]["attempts"] == 2


def test_old_claim_token_cannot_acknowledge_new_attempt(store):
    enqueue(frame(1))
    old_key, _, old_token = durable._claim(SCOPE)
    expire_claims(store)
    durable.recover_stale_claims(scope=SCOPE)
    durable.resolve_notification(scope=SCOPE, job_key=old_key, expected_attempts=1,
                                 decision="pending", acknowledge_duplicate_risk=True)
    new_key, _, new_token = durable._claim(SCOPE)
    assert old_key == new_key and old_token != new_token
    with pytest.raises(durable.StateConflict):
        durable._acknowledge(SCOPE, old_key, old_token, "sent")
    durable._acknowledge(SCOPE, new_key, new_token, "sent")
    assert durable.list_notifications(scope=SCOPE, state="sent")[0]["attempts"] == 2


def test_old_operator_attempt_cannot_resolve_new_failure(store):
    enqueue(frame(1))
    notify = Mock(side_effect=TimeoutError)
    durable.dispatch_outbox(scope=SCOPE, notify=notify)
    key = durable.list_notifications(scope=SCOPE)[0]["job_key"]
    durable.resolve_notification(scope=SCOPE, job_key=key, expected_attempts=1,
                                 decision="pending", acknowledge_duplicate_risk=True)
    durable.dispatch_outbox(scope=SCOPE, notify=notify)
    with pytest.raises(durable.StateConflict):
        durable.resolve_notification(scope=SCOPE, job_key=key, expected_attempts=1, decision="sent")
    assert durable.list_notifications(scope=SCOPE)[0]["attempts"] == 2


def test_acknowledgement_db_failure_does_not_replay_callback(store, monkeypatch):
    enqueue(frame(1))
    original = durable._acknowledge
    monkeypatch.setattr(durable, "_acknowledge", Mock(side_effect=PipelineError("acknowledgement")))
    notify = Mock()
    with pytest.raises(PipelineError):
        durable.dispatch_outbox(scope=SCOPE, notify=notify)
    monkeypatch.setattr(durable, "_acknowledge", original)
    assert durable.dispatch_outbox(scope=SCOPE, notify=notify).sent == 0
    notify.assert_called_once()
    assert len(durable.list_notifications(scope=SCOPE, state="inflight")) == 1


def test_scope_values_are_bound_and_metadata_listing_is_bounded(store):
    hostile_scope = "synthetic'; DROP TABLE hhmur_results_v1; --"
    enqueue(scope=hostile_scope)
    assert counts(store) == (2, 2)
    assert len(durable.list_notifications(scope=hostile_scope, state="pending", limit=1)) == 1
    assert durable.dispatch_outbox(scope=SCOPE, notify=Mock()).sent == 0
    assert durable.dispatch_outbox(scope=hostile_scope, notify=Mock()).sent == 2
