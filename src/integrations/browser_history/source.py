"""Firefox/Zen extraction, shared by retained artifacts and the legacy folder wrapper."""

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import dlt
from dlt.extract import DltResource

from src.integrations.browser_history.formats import readonly

SNAPSHOT = "zen-places.sqlite.gz"


@dlt.source(name="browser_history")
def browser_snapshot_source(
    snapshot_path: str, incremental: bool = False
) -> list[DltResource]:
    return _resources(lambda: snapshot_path, incremental)


def _resources(
    snapshot_path: Callable[[], str], incremental: bool
) -> list[DltResource]:
    def rows(sql: str, *params: Any) -> Iterator[dict[str, Any]]:
        db = readonly(Path(snapshot_path()))
        db.row_factory = sqlite3.Row
        db.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
        try:
            table = sql.split("from ", 1)[1].split()[0]
            # Optional Firefox interaction/bookmark tables vary with profile/version.
            if db.execute(
                "select 1 from sqlite_schema where name=? and type='table'", (table,)
            ).fetchone():
                for row in db.execute(sql, params):
                    yield dict(row)
        finally:
            db.close()

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_places(
        last_visit: dlt.sources.incremental[int] | None = dlt.sources.incremental(  # noqa: B008
            "last_visit_date", initial_value=0, on_cursor_value_missing="include"
        )
        if incremental
        else None,
    ) -> Iterator[dict[str, Any]]:
        """moz_places: one row per URL (title, visit count, frecency...)."""
        if last_visit is None:
            yield from rows("select * from moz_places")
        else:
            yield from rows(
                "select * from moz_places where last_visit_date >= ? or last_visit_date is null",
                last_visit.last_value,
            )

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_historyvisits(
        visit_date: dlt.sources.incremental[int] | None = dlt.sources.incremental(  # noqa: B008
            "visit_date", initial_value=0
        )
        if incremental
        else None,
    ) -> Iterator[dict[str, Any]]:
        """moz_historyvisits: one row per visit (place_id, from_visit, visit_type, source)."""
        yield from rows(
            "select * from moz_historyvisits"
            if visit_date is None
            else "select * from moz_historyvisits where visit_date >= ?",
            *(() if visit_date is None else (visit_date.last_value,)),
        )

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_places_metadata(
        updated_at: dlt.sources.incremental[int] | None = dlt.sources.incremental(  # noqa: B008
            "updated_at", initial_value=0
        )
        if incremental
        else None,
    ) -> Iterator[dict[str, Any]]:
        """moz_places_metadata: per page interaction (total_view_time, typing_time, key_presses...)."""
        yield from rows(
            "select * from moz_places_metadata"
            if updated_at is None
            else "select * from moz_places_metadata where updated_at >= ?",
            *(() if updated_at is None else (updated_at.last_value,)),
        )

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
