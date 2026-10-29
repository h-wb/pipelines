"""Firefox/Zen extraction from one prepared, read-only Places snapshot."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dlt
from dlt.extract import DltResource

from src.integrations.browser_history.formats import readonly


@dlt.source(name="browser_history")
def browser_snapshot_source(snapshot_path: str) -> list[DltResource]:
    def rows(sql: str) -> Iterator[dict[str, Any]]:
        db = readonly(Path(snapshot_path))
        db.row_factory = sqlite3.Row
        db.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
        try:
            table = sql.split("from ", 1)[1].split()[0]
            # Optional Firefox interaction/bookmark tables vary with profile/version.
            if db.execute(
                "select 1 from sqlite_schema where name=? and type='table'", (table,)
            ).fetchone():
                for row in db.execute(sql):
                    yield dict(row)
        finally:
            db.close()

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_places() -> Iterator[dict[str, Any]]:
        """moz_places: one row per URL (title, visit count, frecency...)."""
        yield from rows("select * from moz_places")

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_historyvisits() -> Iterator[dict[str, Any]]:
        """moz_historyvisits: one row per visit (place_id, from_visit, visit_type, source)."""
        yield from rows("select * from moz_historyvisits")

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_places_metadata() -> Iterator[dict[str, Any]]:
        """moz_places_metadata: per page interaction (view, typing, scrolling time...)."""
        yield from rows("select * from moz_places_metadata")

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_origins() -> Iterator[dict[str, Any]]:
        """moz_origins: one row per scheme + host."""
        yield from rows("select * from moz_origins")

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_bookmarks() -> Iterator[dict[str, Any]]:
        """moz_bookmarks: bookmarks and folders (fk -> moz_places.id)."""
        yield from rows("select * from moz_bookmarks")

    return [
        zen_places,
        zen_historyvisits,
        zen_places_metadata,
        zen_origins,
        zen_bookmarks,
    ]
