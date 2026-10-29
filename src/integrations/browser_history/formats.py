"""Bounded, read-only preparation of untrusted Firefox Places snapshots. No dlt."""

import gzip
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
REQUIRED = {
    "moz_places": {"id", "url"},
    "moz_historyvisits": {"id", "place_id", "visit_date"},
}


def readonly(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(
        f"file:{quote(str(path.resolve()), safe='/')}?mode=ro&immutable=1", uri=True
    )
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA trusted_schema=OFF")
    return db


@contextmanager
def open_snapshot(path: Path) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="miroir-browser-") as tmp:
        target = Path(tmp) / "places.sqlite"
        with path.open("rb") as raw:
            compressed = raw.read(2) == b"\x1f\x8b"
        opener = gzip.open if compressed else open
        size = 0
        with opener(path, "rb") as source, target.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_SNAPSHOT_BYTES:
                    raise ValueError("Snapshot exceeds uncompressed size limit")
                output.write(chunk)
        try:
            db = readonly(target)
            try:
                objects = dict(
                    db.execute(
                        "select name,type from sqlite_schema where name not like 'sqlite_%'"
                    )
                )
                for table, required in REQUIRED.items():
                    if objects.get(table) != "table":
                        raise ValueError("Missing Places table")
                    columns = {
                        row[1] for row in db.execute(f'PRAGMA table_info("{table}")')
                    }
                    if required - columns:
                        raise ValueError("Missing Places columns")
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("Snapshot integrity check failed")
            finally:
                db.close()
        except sqlite3.Error as error:
            raise ValueError("Invalid Places database") from error
        yield target
