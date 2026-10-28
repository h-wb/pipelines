"""Apple Health data source from Auto Export JSON files."""

import json

import dlt
from dlt.sources.filesystem import filesystem

# Auto Export collection -> (table, primary key, timestamp field compared to the cutoff)
TABLES = {
    "metrics": ("metrics", ["metric_name", "date", "source"], "date"),
    "workouts": ("workouts", "id", "start"),
    "stateOfMind": ("state_of_mind", "id", "start"),
    "medications": ("medications", ["displayText", "start"], "start"),
}


@dlt.source(name="apple_health")
def apple_health(data_dir: str = dlt.config.value, cutoff: str = dlt.config.value):
    """Apple Health source reading Auto Export JSON files.

    Files under ``manual/`` are the one-off bulk export and only contribute rows
    dated before ``cutoff`` (YYYY-MM-DD). All other files come from the automatic
    export and only contribute rows from ``cutoff`` onwards. Files are tracked by
    modification date, so each one is only read once.
    """
    files = filesystem(
        bucket_url=data_dir,
        file_glob="**/*.json",
        incremental=dlt.sources.incremental("modification_date"),
    )

    @dlt.transformer(name="apple_health_rows")
    def rows(items):
        for item in items:
            manual = "/manual/" in item["file_url"]
            yield from select_rows(json.loads(item.read_bytes())["data"], cutoff, manual)

    return files | rows


@dlt.source(name="apple_health")
def apple_health_artifact(path: str, cutoff: str, historical: bool):
    """One explicitly selected Auto Export file, with no modification-time cursor.

    ``historical`` replaces the ``manual/`` path convention: True keeps rows dated
    before ``cutoff``, False keeps rows from ``cutoff`` onwards.
    """

    @dlt.resource(name="apple_health_artifact")
    def rows():
        with open(path, "rb") as handle:
            data = json.load(handle)["data"]
        yield from select_rows(data, cutoff, historical)

    return rows


def select_rows(data, cutoff, historical):
    """Rows of one export on the selected side of the cutoff, with table hints."""
    for key, (table, primary_key, ts_field) in TABLES.items():
        hints = dlt.mark.make_hints(
            table_name=table, primary_key=primary_key, write_disposition="merge"
        )
        for row in _rows(key, data.get(key, [])):
            # Local date prefix of "YYYY-MM-DD HH:MM:SS -ZZZZ"
            if (row[ts_field][:10] < cutoff) != historical:
                continue
            yield dlt.mark.with_hints(row, hints)


def _rows(key, entries):
    if key != "metrics":
        yield from entries
        return
    for metric in entries:
        name, units = metric["name"], metric.get("units")
        for point in metric.get("data", []):
            # Some auto-export points have no source, which is part of the primary key
            yield {"metric_name": name, "units": units, **point, "source": point.get("source") or "unknown"}
