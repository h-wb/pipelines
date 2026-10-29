"""Health Auto Export extraction from one stored file and its import role."""

import json
from collections.abc import Iterator
from functools import lru_cache
from typing import Any, cast

import dlt
from dlt.extract import DltResource, DltSource

TABLES = {
    "metrics": ("metrics", ["metric_name", "date", "source"], "date"),
    "workouts": ("workouts", "id", "start"),
    "stateOfMind": ("state_of_mind", "id", "start"),
    "medications": ("medications", ["displayText", "start"], "start"),
}


@dlt.source(name="apple_health")
def health_files(files: list[tuple[str, bool]], cutoff: str) -> list[DltResource]:
    @lru_cache(maxsize=1)
    def document(path: str) -> dict[str, Any]:
        with open(path, "rb") as reader:
            return cast(dict[str, Any], json.load(reader)["data"])

    def resource(collection: str, table: str, key: Any, date_field: str) -> DltResource:
        @dlt.resource(name=table, primary_key=key, write_disposition="merge")
        def rows() -> Iterator[dict[str, Any]]:
            for path, historical in files:
                entries = document(path).get(collection, [])
                if collection == "metrics":
                    records = (
                        {
                            "metric_name": metric["name"],
                            "units": metric.get("units"),
                            **point,
                            "source": point.get("source") or "unknown",
                        }
                        for metric in entries
                        for point in metric.get("data", [])
                    )
                else:
                    records = iter(entries)
                for row in records:
                    if (row[date_field][:10] < cutoff) == historical:
                        yield row

        return rows

    return [
        resource(collection, table, key, date_field)
        for collection, (table, key, date_field) in TABLES.items()
    ]


def apple_health_artifact(path: str, cutoff: str, historical: bool) -> DltSource:
    return health_files(files=[(path, historical)], cutoff=cutoff)
