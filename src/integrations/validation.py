"""Validate submitted files before they become artifacts. Stdlib only.

Validation checks structure, not every record: the parser still fails loudly on
records it cannot interpret, and that failure is recorded on the import attempt.
"""

import json
from pathlib import Path


class InvalidArtifact(ValueError):
    """The file is not in the integration's input format. Never echoes contents."""


HEALTH_COLLECTIONS = ("metrics", "workouts", "stateOfMind", "medications")


def _health_auto_export_json(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            document = json.load(handle)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        raise InvalidArtifact("Not a readable JSON document") from error
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, dict):
        raise InvalidArtifact("Expected a top-level 'data' object")
    present = [key for key in HEALTH_COLLECTIONS if key in data]
    if not present:
        raise InvalidArtifact("No Health Auto Export collections found")
    for key in present:
        entries = data[key]
        if not isinstance(entries, list) or not all(
            isinstance(entry, dict) for entry in entries
        ):
            raise InvalidArtifact(f"'{key}' must be a list of objects")
