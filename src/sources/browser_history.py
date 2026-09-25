"""Browser history source (extract only; transforms live in dbt).

Zen (Firefox-based) keeps its history in places.sqlite. A mise task in the
dotfiles (conf.d/zen-history.personal.toml) drops a consistent gzipped copy into
Nextcloud's Documents/data/browser every 6h; that folder is the NAS share the
Prefect worker mounts at /root/.prefect/data. This source opens the snapshot and
loads its tables raw as `zen_*`, incrementally where the table has a usable
cursor. Firefox trims old history as the DB grows; merged rows stay.

The Arc history from before the switch to Zen was imported into Zen, so it is
part of places.sqlite. Timestamps are left as stored: microseconds since the
epoch (PRTime).
"""

import gzip
import os
import shutil
import sqlite3
import tempfile
from typing import Any, Dict, Iterator

import dlt
from dlt.extract import DltResource

SNAPSHOT = "zen-places.sqlite.gz"


@dlt.source(name="browser_history")
def browser_history_source(
    data_dir: str = dlt.config.value,
) -> Iterator[DltResource]:
    """Args:
        data_dir: folder holding the snapshot
    """
    snapshot: Dict[str, str] = {}

    def snapshot_path() -> str:
        """Decompress the snapshot once per run into a scratch file."""
        if "path" not in snapshot:
            workdir = tempfile.mkdtemp(prefix="browser_history_")
            snapshot["path"] = os.path.join(workdir, "places.sqlite")
            with gzip.open(os.path.join(data_dir, SNAPSHOT)) as src, open(snapshot["path"], "wb") as dst:
                shutil.copyfileobj(src, dst)
        return snapshot["path"]

    def rows(sql: str, *params: Any) -> Iterator[Dict[str, Any]]:
        # read-only: the scratch copy is never written back
        db = sqlite3.connect(f"file:{snapshot_path()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        # page titles/descriptions are stored as sites sent them, not always valid UTF-8
        db.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
        try:
            for row in db.execute(sql, params):
                yield dict(row)
        finally:
            db.close()

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_places(
        last_visit=dlt.sources.incremental("last_visit_date", initial_value=0, on_cursor_value_missing="include"),
    ) -> Iterator[Dict[str, Any]]:
        """moz_places: one row per URL (title, visit count, frecency...)."""
        yield from rows(
            "select * from moz_places where last_visit_date >= ? or last_visit_date is null",
            last_visit.last_value,
        )

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_historyvisits(
        visit_date=dlt.sources.incremental("visit_date", initial_value=0),
    ) -> Iterator[Dict[str, Any]]:
        """moz_historyvisits: one row per visit (place_id, from_visit, visit_type, source)."""
        yield from rows("select * from moz_historyvisits where visit_date >= ?", visit_date.last_value)

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_places_metadata(
        updated_at=dlt.sources.incremental("updated_at", initial_value=0),
    ) -> Iterator[Dict[str, Any]]:
        """moz_places_metadata: per page interaction (total_view_time, typing_time, key_presses...)."""
        yield from rows("select * from moz_places_metadata where updated_at >= ?", updated_at.last_value)

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_origins() -> Iterator[Dict[str, Any]]:
        """moz_origins: one row per scheme + host."""
        yield from rows("select * from moz_origins")

    @dlt.resource(write_disposition="merge", primary_key="id")
    def zen_bookmarks() -> Iterator[Dict[str, Any]]:
        """moz_bookmarks: bookmarks and folders (fk -> moz_places.id)."""
        yield from rows("select * from moz_bookmarks")

    return [zen_places, zen_historyvisits, zen_places_metadata, zen_origins, zen_bookmarks]
