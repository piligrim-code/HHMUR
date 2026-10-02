"""Synthetic pipeline regressions; no credentials or external services."""
import json
from pathlib import Path
import subprocess
import sys
import traceback
from unittest.mock import Mock

import pandas as pd
import pytest

from api import db
from api.main import demo, run_database_pipeline
from api.pipeline import Evaluation, PipelineError, parse_evaluation, run_pipeline


def frame(index=(41, 7, 41)):
    return pd.DataFrame([
        [f"role-{i}", "synthetic", None, "Remote", pd.NA,
         f"description-{i}", f"https://example.invalid/{i}"]
        for i in range(len(index))
    ], columns=db.VACANCY_COLUMNS, index=index, dtype=object)


def adapters():
    return dict(evaluate=Mock(return_value=Evaluation("Synthetic reply", 1)),
                persist=Mock(), notify=Mock())


def test_order_alignment_and_original_are_preserved():
    source = frame()
    original = source.copy(deep=True)
    events = []
    scores = iter([1, 0, 1])
    def evaluate(title, description):
        events.append("evaluate")
        return Evaluation(f"{title}: {description}", next(scores))
    def persist(result):
        events.append("persist")
        assert result.index.tolist() == [41, 7, 41]
        assert list(result.columns) == list(db.READY_COLUMNS)
        assert result[db.READY_COLUMNS[-2]].tolist() == [
            f"role-{i}: description-{i}" for i in range(3)]
        assert result[db.READY_COLUMNS[-1]].tolist() == [1, 0, 1]
    messages = []
    def notify(message):
        events.append("notify")
        messages.append(message)
    report = run_pipeline(source, evaluate=evaluate, persist=persist, notify=notify)
    assert events == ["evaluate"] * 3 + ["persist", "notify", "notify"]
    assert "role-0" in messages[0] and "role-2" in messages[1]
    assert report.persisted == 3 and report.accepted == 2
    assert report.notifications_sent == 2 and report.notification_failures == ()
    pd.testing.assert_frame_equal(source, original)


def test_aliases_and_extra_id_are_normalized():
    source = frame().rename(columns={target: alias for alias, target in db.COLUMN_ALIASES.items()})
    source["id"] = [1, 2, 3]
    calls = adapters()
    run_pipeline(source, **calls)
    assert list(calls["persist"].call_args.args[0].columns) == list(db.READY_COLUMNS)


def test_empty_batch_has_no_side_effects():
    calls = adapters()
    report = run_pipeline(frame(index=()), **calls)
    assert report.persisted == report.accepted == report.notifications_sent == 0
    for callback in calls.values():
        callback.assert_not_called()


@pytest.mark.parametrize("value", [None, 1, True, [], "", " ", "bad\x00text", "\ud800"])
@pytest.mark.parametrize("column", [0, 5, 6])
def test_bad_required_field_prevents_all_side_effects(value, column):
    source = frame()
    source.iat[2, column] = value
    calls = adapters()
    with pytest.raises(ValueError):
        run_pipeline(source, **calls)
    for callback in calls.values():
        callback.assert_not_called()


@pytest.mark.parametrize("value", [1, [], {}, "bad\x00text", "\ud800"])
def test_bad_optional_field_prevents_evaluation(value):
    source = frame()
    source.iat[1, 1] = value
    calls = adapters()
    with pytest.raises(ValueError):
        run_pipeline(source, **calls)
    calls["evaluate"].assert_not_called()


@pytest.mark.parametrize("kind", ["missing", "duplicate", "alias", "not-frame", "too-many"])
def test_invalid_batch_is_rejected(kind):
    source = frame()
    if kind == "missing":
        source = source.drop(columns=db.VACANCY_COLUMNS[1])
    elif kind == "duplicate":
        source = pd.concat([source, source.iloc[:, :1]], axis=1)
    elif kind == "alias":
        source[next(iter(db.COLUMN_ALIASES))] = "duplicate"
    elif kind == "not-frame":
        source = []
    calls = adapters()
    with pytest.raises(ValueError):
        run_pipeline(source, **calls, max_rows=2 if kind == "too-many" else 1000)
    for callback in calls.values():
        callback.assert_not_called()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, None])
def test_bad_limit(limit):
    calls = adapters()
    with pytest.raises(ValueError):
        run_pipeline(frame(), **calls, max_rows=limit)
    calls["evaluate"].assert_not_called()


@pytest.mark.parametrize("name", ["evaluate", "persist", "notify"])
def test_bad_adapter(name):
    calls = adapters()
    calls[name] = "not callable"
    with pytest.raises(ValueError):
        run_pipeline(frame(), **calls)


@pytest.mark.parametrize("score", [None, True, False, 1.0, "1", -1, 2, 10])
def test_nonbinary_or_untyped_scores_rejected(score):
    with pytest.raises(ValueError):
        Evaluation("Synthetic reply", score)


@pytest.mark.parametrize("response", [None, "", " ", 1, "bad\x00reply", "\ud800", "x" * 16001])
def test_bad_response_rejected(response):
    with pytest.raises(ValueError):
        Evaluation(response, 0)


@pytest.mark.parametrize("response,score", [
    ("Score: 1", 1), ("Reason\nScore: 0\n", 0),
    ("\u041e\u0446\u0435\u043d\u043a\u0430: 1\r\nReason", 1),
    ("  score:\t0 \r\n", 0),
])
def test_strict_parser_accepts_one_binary_score(response, score):
    assert parse_evaluation(response) == Evaluation(response, score)


@pytest.mark.parametrize("response", [
    None, "", "No score", "Score: 10", "Score: 2", "Score: 1.0",
    "Score: -1", "Score: 1 (maybe)", "Score: 1\nScore: 0",
    "Score: 1\nScore: 1", "Score: 1\nScore: invalid",
    "Score: 1\n\u041e\u0446\u0435\u043d\u043a\u0430: 0",
    "prefix Score: 1", "Score: 1\x00", "Score: 1\n\ud800",
])
def test_strict_parser_never_coerces_bad_scores_to_rejection(response):
    with pytest.raises(ValueError):
        parse_evaluation(response)


@pytest.mark.parametrize("bad", [RuntimeError("synthetic-private-detail"), None, "Score: 1"])
def test_evaluator_failure_aborts_entire_batch_without_leaking_details(bad, capsys):
    calls = adapters()
    calls["evaluate"].side_effect = [Evaluation("First reply", 1), bad]
    with pytest.raises(PipelineError) as caught:
        run_pipeline(frame(), **calls)
    assert caught.value.stage == "evaluation" and caught.value.position == 1
    assert calls["evaluate"].call_count == 2
    calls["persist"].assert_not_called()
    calls["notify"].assert_not_called()
    assert "synthetic-private-detail" not in "".join(traceback.format_exception(caught.value))
    assert capsys.readouterr() == ("", "")


def test_persistence_failure_never_notifies_or_retries():
    calls = adapters()
    calls["persist"].side_effect = RuntimeError("synthetic-private-detail")
    with pytest.raises(PipelineError) as caught:
        run_pipeline(frame(), **calls)
    assert caught.value.stage == "persistence"
    calls["persist"].assert_called_once()
    calls["notify"].assert_not_called()
    assert "synthetic-private-detail" not in "".join(traceback.format_exception(caught.value))


def test_notification_failures_do_not_erase_commit_or_replay():
    calls = adapters()
    calls["notify"].side_effect = [RuntimeError("synthetic-private-detail"), None, TimeoutError()]
    report = run_pipeline(frame(), **calls)
    calls["persist"].assert_called_once()
    assert calls["notify"].call_count == 3
    assert report.persisted == 3 and report.notifications_sent == 1
    assert report.notification_failures == (0, 2)
    assert report.notifications_skipped == 0


def test_disabled_notifications_are_explicit():
    calls = adapters()
    calls["notify"] = None
    report = run_pipeline(frame(), **calls)
    assert report.notifications_sent == 0 and report.notifications_skipped == 3


def test_rejections_are_saved_without_notifications():
    calls = adapters()
    calls["evaluate"].return_value = Evaluation("Rejected synthetic row", 0)
    report = run_pipeline(frame(), **calls)
    assert report.persisted == 3 and report.accepted == 0
    calls["notify"].assert_not_called()


def test_unicode_notifications_are_bounded_without_truncating_saved_reply():
    calls = adapters()
    response = "\U0001f600" * 16000
    calls["evaluate"].return_value = Evaluation(response, 1)
    run_pipeline(frame(index=(99,)), **calls)
    message = calls["notify"].call_args.args[0]
    assert len(message.encode("utf-16-le")) <= 7000
    assert calls["persist"].call_args.args[0][db.READY_COLUMNS[-2]].iloc[0] == response


def test_keyboard_interrupt_is_not_swallowed():
    calls = adapters()
    calls["evaluate"].side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(frame(), **calls)
    calls["persist"].assert_not_called()


def test_database_entry_point_uses_injected_adapters(monkeypatch):
    reader, writer = Mock(return_value=frame()), Mock()
    monkeypatch.setattr(db, "export_dataframe_from_postgresql", reader)
    monkeypatch.setattr(db, "import_dataframe_to_postgresql_ready", writer)
    report = run_database_pipeline(evaluate=lambda title, description: Evaluation(title, 0),
                                   source="synthetic_input", destination="synthetic_output")
    reader.assert_called_once_with("synthetic_input")
    assert writer.call_count == 1 and writer.call_args.args[1] == "synthetic_output"
    assert report.persisted == 3


@pytest.mark.parametrize("source,destination", [("same", "same"), ("", "output"), ("input", "x" * 64)])
def test_database_entry_point_validates_names_before_loading(monkeypatch, source, destination):
    reader = Mock()
    monkeypatch.setattr(db, "export_dataframe_from_postgresql", reader)
    with pytest.raises(ValueError):
        run_database_pipeline(evaluate=Mock(), source=source, destination=destination)
    reader.assert_not_called()


@pytest.mark.parametrize("options", [{"evaluate": None}, {"notify": 1}, {"max_rows": 0}, {"max_rows": True}])
def test_database_options_are_validated_before_connecting(monkeypatch, options):
    reader = Mock()
    monkeypatch.setattr(db, "export_dataframe_from_postgresql", reader)
    arguments = {"evaluate": Mock(), **options}
    with pytest.raises(ValueError):
        run_database_pipeline(**arguments)
    reader.assert_not_called()


def test_load_failure_is_sanitized(monkeypatch):
    monkeypatch.setattr(db, "export_dataframe_from_postgresql",
                        Mock(side_effect=RuntimeError("synthetic-private-detail")))
    with pytest.raises(PipelineError) as caught:
        run_database_pipeline(evaluate=Mock())
    assert caught.value.stage == "load"
    assert "synthetic-private-detail" not in "".join(traceback.format_exception(caught.value))


def test_demo_is_repeatable():
    assert demo() == demo() == {
        "mode": "synthetic-offline", "persisted": 2, "accepted": 1,
        "notifications_sent": 1, "notification_failures": (),
        "notifications_skipped": 0,
    }


@pytest.mark.parametrize("args,code", [(["--demo"], 0), (["--help"], 0), ([], 2)])
def test_import_and_cli_never_load_legacy_integrations_or_connect(args, code):
    # Install guards before import; CLI checks are run in a fresh interpreter.
    script = """
import builtins, socket, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'model', 'telebot', 'dotenv', 'google', 'llama_cpp'}:
        raise AssertionError('legacy import attempted')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
def forbidden(*args, **kwargs):
    raise AssertionError('network attempted')
socket.socket.connect = forbidden
socket.create_connection = forbidden
import psycopg2
psycopg2.connect = forbidden
from api.main import main
import api.durable
import api.providers
raise SystemExit(main(sys.argv[1:]))
"""
    result = subprocess.run([sys.executable, "-c", script, *args],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == code, result.stderr
    if args == ["--demo"]:
        assert json.loads(result.stdout)["persisted"] == 2
