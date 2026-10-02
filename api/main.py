"""Explicit pipeline entry points; importing this module starts no services."""
import argparse
from dataclasses import asdict
import json

import pandas as pd

from api import db
from api.pipeline import Evaluation, PipelineError, _validate_options, run_pipeline


def run_database_pipeline(*, evaluate, notify=None, source="vacancies",
                          destination="vacancies_ready", max_rows=1000):
    """Read the configured DB only when explicitly called by an application."""
    _validate_options(evaluate, db.import_dataframe_to_postgresql_ready, notify, max_rows)
    db._table_identifier(source)
    db._table_identifier(destination)
    if source == destination:
        raise ValueError("Source and destination must differ")
    try:
        vacancies = db.export_dataframe_from_postgresql(source)
    except Exception:
        raise PipelineError("load") from None
    return run_pipeline(
        vacancies, evaluate=evaluate,
        persist=lambda frame: db.import_dataframe_to_postgresql_ready(frame, destination),
        notify=notify, max_rows=max_rows,
    )


def demo():
    """Synthetic, deterministic in-memory demonstration, not model inference."""
    vacancies = pd.DataFrame([
        ["Synthetic Python role", "Example A", "Any", "Remote", "Python",
         "Synthetic backend task", "https://example.invalid/jobs/1"],
        ["Synthetic other role", "Example B", "Any", "Remote", "Other",
         "Synthetic other task", "https://example.invalid/jobs/2"],
    ], columns=db.VACANCY_COLUMNS, index=[42, 7])
    saved, messages = [], []

    def evaluate(title, description):
        return Evaluation("Synthetic rule-based reply; no model was called.",
                          int(title == "Synthetic Python role"))

    report = run_pipeline(vacancies, evaluate=evaluate,
                          persist=lambda frame: saved.append(frame.copy()),
                          notify=messages.append)
    return {"mode": "synthetic-offline", **asdict(report)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="HHMUR synthetic pipeline demonstration")
    parser.add_argument("--demo", action="store_true", required=True,
                        help="Use only synthetic rows and in-memory adapters")
    parser.parse_args(argv)
    print(json.dumps(demo(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
