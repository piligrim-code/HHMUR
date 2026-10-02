"""Opt-in PostgreSQL result deduplication and a no-automatic-replay outbox."""
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import inspect
import json
import re
import uuid

import pandas as pd
from psycopg2.extras import Json

from api import db
from api.pipeline import (
    Evaluation, PipelineError, _notification, _valid_text, _validated_frame,
    _validate_options,
)


@dataclass(frozen=True)
class DurableReport:
    input_rows: int
    duplicates_in_batch: int
    already_committed: int
    concurrent_skips: int
    committed: int
    queued: int


@dataclass(frozen=True)
class DispatchReport:
    sent: int
    uncertain: int


class StateConflict(RuntimeError):
    """A stale claim or operator decision must not overwrite newer state."""


def _scope(value):
    if not _valid_text(value) or not value.strip() or len(value.encode("utf-8")) > 128:
        raise ValueError("scope must be nonempty UTF-8 text, at most 128 bytes")


def _positive(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")


def _require_sync(callback):
    if (not callable(callback) or inspect.iscoroutinefunction(callback)
            or inspect.iscoroutinefunction(getattr(callback, "__call__", None))):
        raise ValueError("Callbacks must be synchronous callables")


def _invoke(callback, *args):
    result = callback(*args)
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise ValueError("Callback returned unfinished asynchronous work")
    return result


@contextmanager
def _transaction(stage):
    try:
        with closing(db._connect()) as connection:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '5s'")
                    cursor.execute("SET LOCAL lock_timeout = '2s'")
                    cursor.execute("SET LOCAL idle_in_transaction_session_timeout = '10s'")
                    yield cursor
    except StateConflict:
        raise
    except Exception:
        raise PipelineError(stage) from None


def initialize_store():
    """Explicit one-time schema creation; never migrates legacy vacancy tables."""
    with _transaction("store_initialization") as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hhmur_results_v1 (
                scope TEXT NOT NULL CHECK (octet_length(scope) BETWEEN 1 AND 128),
                job_key TEXT NOT NULL CHECK (job_key ~ '^[0-9a-f]{64}$'),
                vacancy JSONB NOT NULL CHECK (
                    jsonb_typeof(vacancy) = 'array' AND jsonb_array_length(vacancy) = 7),
                response TEXT NOT NULL CHECK (length(response) BETWEEN 1 AND 16000),
                score SMALLINT NOT NULL CHECK (score IN (0, 1)),
                created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                PRIMARY KEY (scope, job_key)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hhmur_outbox_v1 (
                scope TEXT NOT NULL,
                job_key TEXT NOT NULL,
                message TEXT NOT NULL CHECK (length(message) BETWEEN 1 AND 3500),
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'inflight', 'sent', 'uncertain', 'abandoned')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                claim_token UUID,
                started_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                reason TEXT CHECK (reason IN ('notification_error', 'stale_claim',
                    'operator_sent', 'operator_retry', 'operator_abandon')),
                PRIMARY KEY (scope, job_key),
                FOREIGN KEY (scope, job_key) REFERENCES hhmur_results_v1(scope, job_key),
                CHECK ((state = 'inflight') = (claim_token IS NOT NULL))
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS hhmur_outbox_pending_v1
            ON hhmur_outbox_v1(scope, updated_at, job_key) WHERE state = 'pending'
        """)


def _key(row):
    # Preserve field boundaries and normalize all pandas missing values to null.
    payload = [None if pd.isna(value) else value for value in row]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest(), payload


def process_durable_batch(vacancies, *, scope, evaluate, max_rows=1000):
    """Evaluate new normalized vacancies and atomically commit results plus messages.

    Scope identifies a policy/prompt version AND notification audience. Change it
    deliberately to re-evaluate. Concurrent workers may both call an evaluator;
    uniqueness prevents duplicate committed results/events, not duplicate API cost.
    """
    _scope(scope)
    _validate_options(evaluate, initialize_store, None, max_rows)
    _require_sync(evaluate)
    frame = _validated_frame(vacancies, max_rows)
    unique = {}
    for position, row in enumerate(frame.itertuples(index=False, name=None)):
        key, payload = _key(row)
        unique.setdefault(key, (position, payload))
    duplicates = len(frame) - len(unique)
    if not unique:
        return DurableReport(0, 0, 0, 0, 0, 0)
    with _transaction("deduplication") as cursor:
        cursor.execute("SELECT job_key FROM hhmur_results_v1 WHERE scope = %s AND job_key = ANY(%s)",
                       (scope, list(unique)))
        existing = {row[0] for row in cursor.fetchall()}
    evaluated = []
    for key, (position, payload) in unique.items():
        if key in existing:
            continue
        try:
            result = _invoke(evaluate, payload[0], payload[5])
            if not isinstance(result, Evaluation):
                raise ValueError("Evaluator must return Evaluation")
            message = _notification(payload[0], payload[6], result) if result.score else None
        except Exception:
            raise PipelineError("evaluation", position) from None
        evaluated.append((key, payload, result, message))
    committed = queued = 0
    if evaluated:
        with _transaction("durable_commit") as cursor:
            # A deterministic insert order avoids reverse-batch unique-key deadlocks.
            for key, payload, result, message in sorted(evaluated, key=lambda item: item[0]):
                cursor.execute("""
                    INSERT INTO hhmur_results_v1 (scope, job_key, vacancy, response, score)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (scope, job_key) DO NOTHING RETURNING job_key
                """, (scope, key, Json(payload), result.response, result.score))
                if cursor.fetchone() is None:
                    continue
                committed += 1
                if message is not None:
                    cursor.execute("""
                        INSERT INTO hhmur_outbox_v1 (scope, job_key, message)
                        VALUES (%s, %s, %s)
                    """, (scope, key, message))
                    queued += 1
    return DurableReport(len(frame), duplicates, len(existing),
                         len(evaluated) - committed, committed, queued)


def _claim(scope):
    token = str(uuid.uuid4())
    with _transaction("notification_claim") as cursor:
        cursor.execute("""
            WITH candidate AS (
                SELECT scope, job_key FROM hhmur_outbox_v1
                WHERE scope = %s AND state = 'pending'
                ORDER BY updated_at, job_key LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            UPDATE hhmur_outbox_v1 AS event
            SET state = 'inflight', claim_token = %s, attempts = attempts + 1,
                started_at = clock_timestamp(), updated_at = clock_timestamp(), reason = NULL
            FROM candidate
            WHERE event.scope = candidate.scope AND event.job_key = candidate.job_key
            RETURNING event.job_key, event.message
        """, (scope, token))
        row = cursor.fetchone()
    return (row[0], row[1], token) if row else None


def _acknowledge(scope, key, token, state):
    reason = "notification_error" if state == "uncertain" else None
    with _transaction("notification_acknowledgement") as cursor:
        cursor.execute("""
            UPDATE hhmur_outbox_v1
            SET state = %s, claim_token = NULL, updated_at = clock_timestamp(), reason = %s
            WHERE scope = %s AND job_key = %s AND state = 'inflight' AND claim_token = %s
            RETURNING job_key
        """, (state, reason, scope, key, token))
        if cursor.fetchone() is None:
            raise StateConflict("Notification claim changed; reconcile delivery before retrying")


def dispatch_outbox(*, scope, notify, limit=100):
    """Attempt pending messages once; callback errors become uncertain, not retries."""
    _scope(scope)
    _positive(limit, "limit", 1000)
    _require_sync(notify)
    sent = uncertain = 0
    for _ in range(limit):
        claim = _claim(scope)
        if claim is None:
            break
        key, message, token = claim
        state = "sent"
        try:
            _invoke(notify, message)
        except Exception:
            state = "uncertain"
        _acknowledge(scope, key, token, state)
        sent += state == "sent"
        uncertain += state == "uncertain"
    return DispatchReport(sent, uncertain)


def recover_stale_claims(*, scope, older_than_seconds=3600):
    """Quarantine interrupted claims. This NEVER makes them pending again."""
    _scope(scope)
    _positive(older_than_seconds, "older_than_seconds", 31536000)
    with _transaction("claim_recovery") as cursor:
        cursor.execute("""
            UPDATE hhmur_outbox_v1 SET state = 'uncertain', claim_token = NULL,
                updated_at = clock_timestamp(), reason = 'stale_claim'
            WHERE scope = %s AND state = 'inflight'
                AND started_at < clock_timestamp() - %s * INTERVAL '1 second'
        """, (scope, older_than_seconds))
        count = cursor.rowcount
    return count


def list_notifications(*, scope, state="uncertain", limit=100):
    """Return bounded metadata only; no vacancy text, replies or message bodies."""
    _scope(scope)
    _positive(limit, "limit", 1000)
    if state not in ("pending", "inflight", "sent", "uncertain", "abandoned"):
        raise ValueError("Invalid notification state")
    with _transaction("notification_inspection") as cursor:
        cursor.execute("""
            SELECT job_key, state, attempts, updated_at, reason FROM hhmur_outbox_v1
            WHERE scope = %s AND state = %s ORDER BY updated_at, job_key LIMIT %s
        """, (scope, state, limit))
        rows = cursor.fetchall()
    return [dict(zip(("job_key", "state", "attempts", "updated_at", "reason"), row)) for row in rows]


def resolve_notification(*, scope, job_key, expected_attempts, decision,
                         acknowledge_duplicate_risk=False):
    """Resolve an uncertain attempt with compare-and-set; explicit retry is risky."""
    _scope(scope)
    if not isinstance(job_key, str) or not re.fullmatch("[0-9a-f]{64}", job_key):
        raise ValueError("Invalid job_key")
    _positive(expected_attempts, "expected_attempts", 2147483647)
    reasons = {"sent": "operator_sent", "pending": "operator_retry", "abandoned": "operator_abandon"}
    if decision not in reasons:
        raise ValueError("decision must be sent, pending or abandoned")
    if decision == "pending" and acknowledge_duplicate_risk is not True:
        raise ValueError("Retry requires acknowledge_duplicate_risk=True")
    with _transaction("notification_resolution") as cursor:
        cursor.execute("""
            UPDATE hhmur_outbox_v1 SET state = %s, reason = %s, updated_at = clock_timestamp()
            WHERE scope = %s AND job_key = %s AND state = 'uncertain' AND attempts = %s
            RETURNING job_key
        """, (decision, reasons[decision], scope, job_key, expected_attempts))
        if cursor.fetchone() is None:
            raise StateConflict("Notification state or attempt changed; inspect it again")
