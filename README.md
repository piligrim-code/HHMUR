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

The default suite uses a mocked PostgreSQL connection and compiles sources without starting
Telegram, browser automation or model clients. They do not certify the complete
scraping/evaluation/notification pipeline. Legacy integrations still need
dependency/configuration review and synthetic end-to-end tests. The database
pipeline module is `api.main`; it requires the legacy integrations and is not
an offline demo. Existing schemas may need a separate reviewed migration.

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
and ASCII/multibyte identifier collisions. These opt-in tests are skipped by
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
