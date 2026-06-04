"""Apple Health data source from Auto Export JSON files."""

import json
from pathlib import Path
import dlt


@dlt.source(name="apple_health")
def apple_health(file_paths: list[str]):
    """Apple Health source reading one or more Auto Export JSON files.

    Accepts a combined export (metrics + workouts in one file) or separate
    metrics and workouts files as produced by the automatic export.
    """

    @dlt.resource(name="metrics", write_disposition="merge", primary_key=["metric_name", "date", "source"])
    def metrics():
        for path in file_paths:
            data = json.loads(Path(path).read_text())["data"]
            for metric in data.get("metrics", []):
                name, units = metric["name"], metric.get("units")
                for point in metric.get("data", []):
                    yield {"metric_name": name, "units": units, **point}

    @dlt.resource(name="workouts", write_disposition="merge", primary_key="id")
    def workouts():
        for path in file_paths:
            data = json.loads(Path(path).read_text())["data"]
            yield from data.get("workouts", [])

    @dlt.resource(name="state_of_mind", write_disposition="merge", primary_key="id")
    def state_of_mind():
        for path in file_paths:
            data = json.loads(Path(path).read_text())["data"]
            yield from data.get("stateOfMind", [])

    @dlt.resource(name="medications", write_disposition="merge", primary_key=["displayText", "start"])
    def medications():
        for path in file_paths:
            data = json.loads(Path(path).read_text())["data"]
            yield from data.get("medications", [])

    return metrics, workouts, state_of_mind, medications
