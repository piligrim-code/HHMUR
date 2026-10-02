# HHMUR

Historical vacancy-processing prototype. This public snapshot is not the
current private application and is not production-qualified.

## Database Configuration

Database operations require an explicit `HHMUR_DATABASE_URL` environment
variable. There is no fallback account or password. Configure it privately
through your deployment environment; do not commit a real value.

`api.db` quotes SQL identifiers, binds values separately, and closes database
connections even after failures. Writes require the declared vacancy columns;
the evaluated-vacancy table also stores response and score fields.
Table names are single quoted identifiers, not SQL or dotted schema paths.
They must be nonempty valid UTF-8, contain no NUL and fit within 63 bytes.
Longer names are rejected before connecting rather than silently resolving to
a different existing table after server-side truncation.

## Offline Regression Tests

Python 3.12 in a virtual environment:

```sh
python -m pip install -r requirements-test.txt
python -m pytest tests -q
```

The default suite tests the adapter with mocked connections and the pipeline
with synthetic evaluators, persistence and notification callbacks. Import/CLI
checks forbid legacy provider imports and network connections. Neither these
tests nor the demo certify real model quality, Telegram delivery or scraping.

## Synthetic Pipeline Demo

```sh
python -m api.main --demo
```

This runs two invented vacancies through a deterministic rule, in-memory
persistence and an in-memory notification sink. It prints only counters:
two persisted, one accepted, one notification captured. It is not an LLM
demonstration and does not connect to PostgreSQL, Telegram or vacancy sites.
No `.env`, prompt file, model weights or historical records are read.
Without `--demo` the CLI exits with usage; imports and `--help` start no services.

`api.pipeline.run_pipeline` accepts a DataFrame and explicit `evaluate`,
`persist` and optional `notify` callbacks. `api.main.run_database_pipeline`
connects that core to the existing database adapter **when explicitly called**.
The caller supplies and owns the evaluator/notifier; there are no live
provider adapters or automatic model downloads on this path.

### Processing Contract

- All required columns and row fields are validated before evaluation. Title,
  description and link must be nonempty text; other declared fields may be
  text or missing. Aliases are normalized and duplicate columns rejected.
  The default maximum is 1,000 rows, configurable with `max_rows`.
- The evaluator is called once per row with `(title, description)` and returns
  `Evaluation(response, score)`. Scores must be integer `0` or `1`, not booleans,
  floats or missing values. Replies must be nonempty UTF-8 text without NUL,
  at most 16,000 characters. `parse_evaluation` optionally parses exactly one
  standalone `Score: 0/1` or Russian-equivalent line; ambiguous replies fail.
- Evaluation failure aborts the batch before any persistence or notification.
  Previously evaluated rows are not saved. Original DataFrame indices,
  including duplicates, cannot shift replies to the wrong vacancy.
- Persistence is called once with the complete batch. Custom callbacks must
  commit atomically; the provided PostgreSQL writer uses one transaction.
  A persistence exception suppresses all notifications.
- Only after successful persistence are accepted rows notified, once each.
  Notification failures do not undo committed rows or stop other notifications.
  The returned `PipelineReport` lists failed **zero-based input positions**,
  sent counts and explicitly skipped notifications when no notifier was given.
  Callers must inspect that report; a return is not proof of complete delivery.
- Notifications are plain text, capped at 3,500 UTF-16 units; replies in the
  database are not truncated. A real notifier must disable markup parsing,
  set its own timeouts and manage client closure. Callbacks own their resources
  and deadlines; the core does not interrupt a hung callback.
- Wrapped load/evaluation/persistence errors expose stage and position rather
  than provider exception text. There is no automatic retry or payload logging.
  These diagnostics do not scrub logs emitted independently by custom callbacks.

### Recovery Limits Of The Simple Pipeline

There is no durable notification outbox, processed marker or deduplication.
Running a database batch again appends duplicate results and may resend messages.
A process crash after commit can leave undelivered notifications; a delivery
timeout may mean the recipient already received the message. A lost commit
acknowledgement is also ambiguous. Inspect durable state before any manual
replay. This is **not** an exactly-once or production recovery guarantee.

The database reader still materializes the entire source table before the row
limit is checked; the limit bounds evaluation calls, not database read memory.
Source order is unspecified. Report positions apply only to that particular
batch and are not stable database identifiers.

## Opt-In Durable Results And Notifications

`api.durable` adds a separate PostgreSQL-backed path with persistent result
deduplication and an outbox. It does not silently change `run_pipeline`,
`run_database_pipeline`, the offline CLI or legacy tables.

- Call `initialize_store()` explicitly once in a reviewed schema.
- Pass validated vacancy batches to `process_durable_batch`, with an explicit
  scope identifying the evaluation policy/version and notification audience.
- Results and accepted-row notifications commit in one transaction. Repeated
  input in the same scope skips already committed evaluations.
- Call `dispatch_outbox` separately with an explicit notification callback.
  Concurrent dispatchers claim different pending events. Failures or interrupted
  attempts never become automatic retries.
- Inspect uncertain deliveries and resolve them explicitly. Requeueing requires
  acknowledgement of duplicate-delivery risk; an external recipient may already
  have received the message.

See `docs/durable-pipeline.md` for the API contract, recovery procedure and
remaining limits. This path still needs separately reviewed live-provider
adapters, access controls and deployment qualification. The default CLI remains
offline; durable-state tests run against owned disposable PostgreSQL in CI.

The root `main.py`, `model.py`, scraper and old `requirements.txt` are historical,
unqualified integrations, not this supported demo path. In particular the
legacy model module has obsolete configuration, implicit downloads and unsafe
retry behavior; do not wire it into the new pipeline unchanged. Install
`requirements-test.txt` for the demo. Existing schemas may need a separate
reviewed migration. This change does not qualify the current private application.

## Disposable PostgreSQL Integration

With the test dependencies installed and a trusted local Linux Docker engine
running (including Docker Desktop's Linux engine on Windows):

```sh
python tools/run_integration.py
```

The runner creates one uniquely named/labeled PostgreSQL container from a
pinned image digest, with generated ephemeral credentials, an automatically
allocated loopback-only port and CPU/memory limits. It ignores any existing
`HHMUR_DATABASE_URL` and inherited libpq `PG*` configuration. It does not read
the project's `.env` or use historical credentials. It rejects SSH/TCP/remote
named-pipe Docker contexts and `DOCKER_HOST` overrides.

Tests bootstrap an empty disposable database, then run the adapter as a login
role without superuser, database-creation or role-creation privileges. Each
case owns a separate schema. All rows, replies, scores and hostile-looking SQL
strings are synthetic. No vacancy site, model API or Telegram service is used.

The suite checks both schemas, appended/reordered rows, empty frames, aliases,
NULLs, numeric zero, quoted values/names, multi-fetch export, full batch/DDL
rollback, incompatible legacy schemas, read-only failures, connection closure
and ASCII/multibyte identifier collisions. Synthetic pipeline cases also check
that separate connections see the full commit before notification, evaluation
failure leaves no output table, failed writes roll back, notification failure
preserves committed results, and empty input causes no adapter calls.
Durable-store tests additionally exercise deduplication, atomic result/outbox
rollback, concurrent writers/dispatchers, interrupted and uncertain deliveries,
explicit recovery and stale claim/operator rejection.
These opt-in tests are skipped by
the default command; CI runs them in a separate Linux job with actual PostgreSQL.

On completion or ordinary failure, the runner removes only its own labeled
container and anonymous volumes. Dependency images remain cached. Hard process
termination or a Docker outage can still prevent cleanup; incomplete cleanup
fails visibly and reports the owned name. Docker access is privileged, not a
sandbox for untrusted code. Output redacts the generated credential, and no
real DSN should be pasted into commands or test fixtures.

## Adapter Boundaries

- Imports append rows, including duplicates. There is no upsert, deduplication,
  update/delete API or idempotency guarantee.
- Vacancy fields, model replies and scores are stored as TEXT. Missing values
  become SQL NULL and export as pandas missing values. A zero score is preserved,
  but numeric ordering/validation is not provided by the TEXT schema.
- Existing incompatible schemas fail instead of being automatically migrated.
  Sequence numbers may have gaps after a transaction rollback.
- Export order is unspecified. Sort by `id` explicitly if order matters.
  `chunksize` controls database fetch batches, not total memory: export still
  materializes the entire result as a DataFrame.
- The adapter has a five-second connection timeout. Query/lock deadlines in the
  disposable fixture are test configuration, not new production guarantees.
- TLS, deployment ACLs, connection pooling, concurrent startup/migrations,
  production load, backup/restore and the complete scraper/model/bot pipeline
  remain unqualified. Python requirements are bounded ranges, not a lock file.

## Security Status

A database connection string was removed from current source in the October 1,
2026 audit. Historical Git commits were not rewritten. If those credentials
were ever used, revoke/rotate them at the database provider; removing a line
from current source is not credential revocation. Do not commit real vacancy
records, applicant messages or environment files. The disposable integration
suite does not confirm provider-side credential revocation, historical-secret
cleanup or production database security. Those remain separate work.
