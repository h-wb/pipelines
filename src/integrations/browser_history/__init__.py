"""Firefox/Zen Places snapshots: delivery and parsing remain independent."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import Field

from src.core.models import StrictModel
from src.integrations import checks
from src.integrations.contracts import (
    FileInput,
    IntegrationDefinition,
    ResourceDefinition,
)
from src.integrations.delivery import DeliveryMethod, FileDelivery


class BrowserHistorySettings(StrictModel):
    data_dir: str | None = Field(
        default=None,
        min_length=1,
        title="Server folder",
        description="Optional folder containing zen-places.sqlite.gz. Uploads do not need a server folder.",
    )


def validate_snapshot(path: Path) -> None:
    from src.integrations.browser_history.formats import open_snapshot
    from src.integrations.validation import InvalidArtifact

    try:
        with open_snapshot(path):
            pass
    except (OSError, ValueError) as error:
        raise InvalidArtifact(
            "Expected a valid Firefox/Zen Places SQLite snapshot, plain or gzip, up to 512 MiB uncompressed"
        ) from error


@contextmanager
def open_artifact(
    settings: Any, credentials: dict[str, str], path: str, options: dict[str, Any]
) -> Iterator[Any]:
    from src.integrations.browser_history.formats import open_snapshot
    from src.integrations.browser_history.source import browser_snapshot_source

    with open_snapshot(Path(path)) as sqlite_path:
        # Every retained snapshot is an explicit input, including older snapshots.
        yield browser_snapshot_source(snapshot_path=str(sqlite_path), incremental=False)


@contextmanager
def open_sync(settings: Any, credentials: dict[str, str]) -> Iterator[Any]:
    from src.core.operations import OperationError
    from src.integrations.browser_history.formats import open_snapshot
    from src.integrations.browser_history.source import (
        SNAPSHOT,
        browser_snapshot_source,
    )

    if settings.data_dir is None:
        raise OperationError("Upload a snapshot or configure a server folder to scan")
    with open_snapshot(Path(settings.data_dir) / SNAPSHOT) as sqlite_path:
        yield browser_snapshot_source(snapshot_path=str(sqlite_path), incremental=True)


DEFINITION = IntegrationDefinition(
    name="browser_history",
    title="Browser history (Firefox / Zen)",
    settings_model=BrowserHistorySettings,
    credentials=(),
    input_modes=("file",),
    capabilities=("directory_import", "artifact_import", "resource_selection"),
    delivery_methods=(
        DeliveryMethod(
            "upload",
            "Upload a snapshot",
            "Import a consistent copy of your Firefox or Zen Places database.",
        ),
        DeliveryMethod(
            "webhook",
            "Send snapshots automatically",
            "Let a device or automation send SQLite snapshots to this connection.",
        ),
        DeliveryMethod(
            "folder",
            "Read a server snapshot",
            "Read zen-places.sqlite.gz from a folder accessible to the worker.",
            ("data_dir",),
            advanced=True,
        ),
    ),
    file_input=FileInput(
        "firefox_places_snapshot",
        1,
        validate_snapshot,
        lambda settings: {},
        open_artifact,
    ),
    file_delivery=FileDelivery(
        label="Firefox / Zen Places snapshot",
        accept=".sqlite,.sqlite.gz,.gz,application/gzip,application/vnd.sqlite3",
        content_type="application/octet-stream",
        sender_instructions="Send a consistent places.sqlite snapshot (optionally gzipped) as the raw HTTP body. Use SQLite's backup tools; do not copy an actively changing database without its WAL.",
        role_help="Snapshots merge observed visits and retain older imported history. Missing interaction tables mean those measurements are unknown.",
        roles=("ongoing",),
        automatic_filename="browser-snapshot.sqlite",
    ),
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
    analysis="browser",
    check=checks.browser_history,
    open_sync=open_sync,
)
