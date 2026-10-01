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

## Offline Regression Tests

Python 3.12 in a virtual environment:

```sh
python -m pip install -r requirements-test.txt
python -m pytest tests -q
```

Tests use a mocked PostgreSQL connection and compile sources without starting
Telegram, browser automation or model clients. They do not certify the complete
scraping/evaluation/notification pipeline. Legacy integrations still need
dependency/configuration review and synthetic end-to-end tests. The database
pipeline module is `api.main`; it requires the legacy integrations and is not
an offline demo. Existing schemas may need a separate reviewed migration.

## Security Status

A database connection string was removed from current source in the October 1,
2026 audit. Historical Git commits were not rewritten. If those credentials
were ever used, revoke/rotate them at the database provider; removing a line
from current source is not credential revocation. Do not commit real vacancy
records, applicant messages or environment files. Live PostgreSQL integration
tests and full historical secret scanning remain outside this corrective patch.
