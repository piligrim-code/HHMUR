"""Evaluate a validated batch, persist once, then attempt notifications once."""
from dataclasses import dataclass
import re
from typing import Callable

import pandas as pd

from api.db import COLUMN_ALIASES, READY_COLUMNS, VACANCY_COLUMNS


@dataclass(frozen=True)
class Evaluation:
    response: str
    score: int

    def __post_init__(self):
        if not _valid_text(self.response) or not self.response.strip() or len(self.response) > 16000:
            raise ValueError("Evaluation response must be nonempty UTF-8 text, at most 16000 characters")
        if type(self.score) is not int or self.score not in (0, 1):
            raise ValueError("Evaluation score must be integer 0 or 1")


@dataclass(frozen=True)
class PipelineReport:
    persisted: int
    accepted: int
    notifications_sent: int
    notification_failures: tuple[int, ...]
    notifications_skipped: int


class PipelineError(RuntimeError):
    """Safe diagnostic without provider errors, row contents or credentials."""

    def __init__(self, stage, position=None):
        self.stage = stage
        self.position = position
        suffix = "" if position is None else f" at row position {position}"
        super().__init__(f"Pipeline {stage} failed{suffix}; no automatic retry")


def _valid_text(value):
    if not isinstance(value, str) or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def parse_evaluation(response):
    """Accept exactly one standalone 'Score: 0/1' or Russian equivalent line."""
    if not _valid_text(response) or len(response) > 16000:
        raise ValueError("Invalid evaluation response")
    labels = re.findall(r"(?:Score|\u041e\u0446\u0435\u043d\u043a\u0430)[ \t]*:", response, re.IGNORECASE)
    matches = re.findall(r"^[ \t]*(?:Score|\u041e\u0446\u0435\u043d\u043a\u0430)[ \t]*:[ \t]*([01])[ \t]*\r?$",
                         response, re.IGNORECASE | re.MULTILINE)
    if len(labels) != 1 or len(matches) != 1:
        raise ValueError("Expected exactly one standalone binary score")
    return Evaluation(response, int(matches[0]))


def _validate_options(evaluate, persist, notify, max_rows):
    if not callable(evaluate) or not callable(persist) or (notify is not None and not callable(notify)):
        raise ValueError("Pipeline adapters must be callable")
    if type(max_rows) is not int or max_rows < 1:
        raise ValueError("max_rows must be a positive integer")


def _validated_frame(vacancies, max_rows):
    if not isinstance(vacancies, pd.DataFrame):
        raise ValueError("Vacancies must be a DataFrame")
    frame = vacancies.rename(columns=COLUMN_ALIASES)
    if frame.columns.duplicated().any() or not set(VACANCY_COLUMNS).issubset(frame.columns):
        raise ValueError("Vacancy columns are missing or duplicated")
    if len(frame) > max_rows:
        raise ValueError("Vacancy batch exceeds max_rows")
    frame = frame.loc[:, list(VACANCY_COLUMNS)].copy()
    required = {0, 5, 6}
    for row in frame.itertuples(index=False, name=None):
        for position, value in enumerate(row):
            if _valid_text(value):
                if position in required and not value.strip():
                    raise ValueError("Title, description and link must be nonempty text")
            elif position in required or not pd.api.types.is_scalar(value) or not pd.isna(value):
                raise ValueError("Vacancy fields must be UTF-8 text or optional missing values")
    return frame


def _notification(title, link, evaluation):
    message = f"Score: 1\nVacancy: {title}\nLink: {link}\nResponse: {evaluation.response}"
    # Bound UTF-16 units as well as characters, including astral Unicode.
    return message.encode("utf-16-le")[:7000].decode("utf-16-le", errors="ignore")


def run_pipeline(vacancies, *, evaluate: Callable[[str, str], Evaluation],
                 persist: Callable[[pd.DataFrame], None],
                 notify: Callable[[str], None] | None = None, max_rows=1000):
    """Callbacks own timeouts/resources; persist must atomically commit the batch.

    Evaluation failures abort before writes. Persistence errors suppress all
    notifications. Notification failures are reported by zero-based position;
    committed rows are never rewritten or retried by this function.
    """
    _validate_options(evaluate, persist, notify, max_rows)
    frame = _validated_frame(vacancies, max_rows)
    if frame.empty:
        return PipelineReport(0, 0, 0, (), 0)
    evaluations = []
    messages = []
    for position, row in enumerate(frame.itertuples(index=False, name=None)):
        try:
            evaluation = evaluate(row[0], row[5])
            if not isinstance(evaluation, Evaluation):
                raise ValueError("Evaluator must return Evaluation")
            evaluations.append(evaluation)
            if evaluation.score == 1:
                messages.append((position, _notification(row[0], row[6], evaluation)))
        except Exception:
            raise PipelineError("evaluation", position) from None
    # Lists assign positionally even when input indices are repeated or unordered.
    frame[READY_COLUMNS[-2]] = [item.response for item in evaluations]
    frame[READY_COLUMNS[-1]] = [item.score for item in evaluations]
    try:
        persist(frame)
    except Exception:
        raise PipelineError("persistence") from None
    failed = []
    sent = 0
    if notify is not None:
        for position, message in messages:
            try:
                notify(message)
                sent += 1
            except Exception:
                failed.append(position)
    return PipelineReport(len(evaluations), len(messages), sent, tuple(failed),
                          len(messages) if notify is None else 0)
