# Durable Pipeline Contract

This opt-in library path supplements the simple synthetic pipeline. It does
not start a worker, migrate legacy tables or connect when imported. It uses
only the explicitly configured `HHMUR_DATABASE_URL`; no `.env` is loaded.
Python 3.12 and `requirements-test.txt` supply its dependencies.

## Initialization And Use

Use a dedicated, reviewed PostgreSQL schema with restricted database access.
Never test against an existing deployment or historical credentials. The
disposable integration runner is the qualification path:

```sh
python tools/run_integration.py
```

An application explicitly calls these APIs in order:

```python
from api.durable import initialize_store, process_durable_batch, dispatch_outbox

# Administrative setup in the intended schema, before starting workers.
initialize_store()

# vacancies is a caller-supplied DataFrame, not a file loaded by this module.
# evaluate(title, description) returns api.pipeline.Evaluation.
# notify(message) must raise on unsuccessful/uncertain delivery.
scope = "policy-v1:audience-A"
result = process_durable_batch(vacancies, scope=scope, evaluate=evaluate)
delivery = dispatch_outbox(scope=scope, notify=notify, limit=100)
```

The example shows API wiring, not an executable provider demo. Construct and
close provider clients outside these calls; callbacks must enforce their own
timeouts and disable markup interpretation for notification text. Returning
normally from `notify` means delivery success to this library. A callback that
only schedules background work must not claim successful delivery this way.
Async callbacks are rejected before claiming work; a wrapper returning an
awaitable is an error, not successful delivery. Use a synchronous adapter.

Initialization creates `hhmur_results_v1` and `hhmur_outbox_v1` plus a partial
pending-event index. It is repeatable for the unchanged v1 schema, not a general
migration or schema-repair mechanism. Run initialization serially before workers.
It never writes to or migrates `vacancies` or `vacancies_ready`. Do not mix the
old append-only writer with this path and assume shared deduplication. Existing
legacy results are not backfilled and may be processed again after adoption.

## Identity And Commit

- The key is SHA-256 of the normalized seven vacancy fields in fixed order.
  Missing values become JSON null; column aliases and DataFrame indices do not
  change identity. Extra database IDs are ignored. URL canonicalization and
  semantic deduplication are not performed. Changed content produces new work.
- The unique result identity is `(scope, job_key)`. Scope MUST distinguish the
  candidate/evaluation policy, prompt/model version and notification audience.
  Reusing a scope with a changed evaluator reuses old results; using a different
  scope deliberately permits evaluation and notifications again. The library
  cannot detect a wrongly reused audience scope or notifier credentials.
- All rows are validated first. Existing results and in-batch duplicates skip
  evaluation. Missing results are evaluated outside database transactions.
  One invalid evaluation aborts all new writes in that batch.
- All new results and their accepted-row messages commit together. Rejections
  are also persisted, with no outbox entry. A write failure rolls back the batch.
  Every inserted result has one unique outbox identity at most.
- Concurrent writers can evaluate the same missing vacancy independently.
  Uniqueness resolves the race at commit: the first committed result wins;
  later writers do not overwrite it or insert another message. This prevents
  duplicate committed work, NOT duplicated inference charges in a race or crash.
- `DurableReport` accounts for input rows, in-batch duplicates, existing results,
  race-lost inserts, committed results and queued notifications. It contains
  no vacancy content. Results retain the full bounded reply; messages retain
  the simple pipeline's 3,500 UTF-16-unit cap.

## Delivery State Machine

`pending -> inflight -> sent` is the normal path. Claim and acknowledgement
are separate transactions. No database connection or row lock is held while
the callback runs. Pending selection uses row locks with `SKIP LOCKED` so
independent dispatchers cannot claim the same pending event simultaneously.

Claims have unique tokens and increasing attempt numbers. Only the holder of
the current token can acknowledge an inflight event. The result is committed
before an event can be claimed; the claim is committed before delivery begins.

- Callback exception: `inflight -> uncertain`. Dispatch continues to other
  pending events, but does not retry this event or log the provider exception.
- Process interruption or failed acknowledgement: the event may remain
  `inflight`. A lost database commit acknowledgement is itself ambiguous.
- `recover_stale_claims` changes old inflight events to `uncertain`, NEVER to
  pending. The default age is one hour, measured by the database clock.
- `DispatchReport` counts sent and uncertain attempts. An acknowledgement or
  claim failure raises a sanitized stage error, stopping that dispatch call.
  Earlier messages in the same call may already have been delivered.

No scheduler or automatic retry loop is installed. Counts are not end-to-end
recipient receipts. A provider can accept a message and then time out, and a
process can stop between send and acknowledgement. Exactly-once external
delivery is not promised.

## Operator Recovery

1. Stop or reconcile any old worker that might still be sending the event.
   An expired claim does not cancel a running callback or external request.
2. Quarantine interrupted claims with `recover_stale_claims(scope=scope,
   older_than_seconds=3600)` when appropriate for the configured provider deadline.
3. Use `list_notifications(scope=scope)` to inspect uncertain-event metadata:
   key, current state, attempt count, timestamp and a fixed reason code. Optional
   state/limit filters support other states. No body or reply is returned.
4. Reconcile actual provider/recipient status outside the library. Then call
   `resolve_notification` with the key and the inspected `expected_attempts`:
   `decision="sent"` for confirmed delivery, `"abandoned"` to stop, or
   `"pending"` for a deliberate new attempt. Requeue requires
   `acknowledge_duplicate_risk=True`; do not infer safe retries from a timeout.
5. A changed state/attempt raises `StateConflict`. Inspect it again rather than
   automatically repeating the operator decision. New claims invalidate old
   acknowledgement tokens. This fencing protects database state, not an old
   worker's ability to complete an external send.

Resolution records the latest state and fixed operator reason, not a complete
historical audit log or authenticated operator identity. Deployment authorization
and a durable operational audit trail remain separate work.

## Operational Boundaries

- The result/outbox tables contain vacancy and reply text; hashing is not
  anonymization. Protect database access, backups, logs and retention policies.
- Removing results/outbox rows, changing scopes, restoring an older backup or
  switching databases can permit repeat evaluation/delivery. There is no TTL
  cleanup or cross-database deduplication.
- Store transactions set 5-second statement, 2-second lock and 10-second idle
  transaction timeouts, in addition to the adapter's connection timeout. These
  are per-transaction controls, not a whole-batch or callback deadline.
- The library accepts bounded caller-supplied batches. It does not add a
  streaming source reader, source-table checkpoint or scheduler. The legacy
  reader still materializes the full table before evaluation-limit checks.
- Runtime SQL uses bound values and fixed table names. Database roles, TLS,
  trusted search paths, concurrent schema migrations, pooling, production load,
  backup recovery and rights to provider/data use are not qualified here.
- The default tests use mocks. Disposable PostgreSQL tests exercise real
  uniqueness, commits, rollback, concurrency and recovery with synthetic rows
  and callbacks. They are not live model or Telegram validation.
