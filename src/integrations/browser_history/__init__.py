"""Firefox/Zen Places snapshots, however they are delivered."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.core.models import StrictModel
from src.integrations.contracts import (
    FileFormat,
    IntegrationDefinition,
    ResourceDefinition,
)


class BrowserHistorySettings(StrictModel):
    """A snapshot carries everything; the profile needs no settings."""


def validate_snapshot(path: Path) -> None:
    from src.integrations.browser_history.formats import open_snapshot
    from src.integrations.validation import InvalidArtifact

    try:
        with open_snapshot(path):
            pass
    except (OSError, ValueError) as error:
        raise InvalidArtifact(
            "Expected a valid Firefox/Zen Places SQLite snapshot, plain or gzip, "
            "up to 512 MiB uncompressed"
        ) from error


@contextmanager
def open_snapshot_source(
    settings: Any, path: str, options: dict[str, Any]
) -> Iterator[Any]:
    from src.integrations.browser_history.formats import open_snapshot
    from src.integrations.browser_history.source import browser_snapshot_source

    with open_snapshot(Path(path)) as sqlite_path:
        # Every snapshot is read in full and merged, however it arrived.
        yield browser_snapshot_source(snapshot_path=str(sqlite_path))


DEFINITION = IntegrationDefinition(
    name="browser_history",
    title="Browser history (Firefox / Zen)",
    settings_model=BrowserHistorySettings,
    credentials=(),
    resources=tuple(
        ResourceDefinition(name, ("id",), cursor, kind)
        for name, cursor, kind in (
            ("zen_places", "last_visit_date", "entity"),
            ("zen_historyvisits", "visit_date", "event"),
            ("zen_places_metadata", "updated_at", "entity"),
            ("zen_origins", None, "entity"),
            ("zen_bookmarks", None, "entity"),
        )
    ),
    formats=(
        FileFormat(
            id="firefox_places_snapshot",
            version=1,
            label="Firefox / Zen Places snapshot",
            accept=".sqlite,.sqlite.gz,.gz,application/gzip,application/vnd.sqlite3",
            content_type="application/octet-stream",
            sender_instructions="Send a consistent places.sqlite snapshot "
            "(optionally gzipped) as the raw HTTP body. Use SQLite's backup tools; "
            "do not copy an actively changing database without its WAL.",
            help="Snapshots merge observed visits and keep older imported history. "
            "Missing interaction tables mean those measurements are unknown.",
            validate=validate_snapshot,
            open=open_snapshot_source,
            default_pattern="*.sqlite.gz",
            received_name="browser-snapshot.sqlite",
        ),
    ),
    analysis="browser",
)
