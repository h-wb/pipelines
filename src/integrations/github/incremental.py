"""One-time bridge from run watermarks to native dlt incremental state."""

from typing import Any

import dlt
from dlt.sources import incremental

INITIAL_TIMESTAMP = "1970-01-01T00:00:00Z"
INITIAL_DATE = "1970-01-01"
LAG_SECONDS = 3 * 24 * 60 * 60


def resume_cursor(
    cursor: incremental[str], starts: dict[str, str], legacy_key: str = "last_run"
) -> str:
    """Seed only an untouched native cursor, inside dlt's extraction transaction."""
    name = cursor.resource_name
    assert name is not None
    # dlt rebinds transformer arguments for each parent; keep one run boundary.
    if name in starts:
        cursor.start_value = starts[name]
        return starts[name]
    state = cursor.get_state()
    legacy: Any = dlt.current.resource_state().get(legacy_key)
    if legacy and state["last_value"] == cursor.initial_value:
        state["last_value"] = str(legacy)
        state["start_value"] = str(legacy)
        cursor.start_value = cursor.last_value
    assert cursor.start_value is not None
    starts[name] = cursor.start_value
    return starts[name]
