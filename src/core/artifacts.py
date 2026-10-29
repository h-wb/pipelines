"""Receive submitted files into durable, content-addressed artifact storage.

Files are copied to a temporary name, flushed to disk, then renamed into place, so
an interrupted copy never appears under an artifact's name. Storage is local for
now; the stored path is recorded so another backend can replace it later.
"""

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.core.config import ResolvedConnection, ResolvedMiroir
from src.core.operations import ArtifactOrigin, MetadataStore, OperationError

CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Received:
    sha256: str
    size: int
    path: Path


def receive_file(
    source: Path, root: Path, miroir_id: str, connection_id: str
) -> Received:
    directory = root / miroir_id / connection_id
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".incoming-")
    try:
        with os.fdopen(handle, "wb") as target, source.open("rb") as reader:
            while chunk := reader.read(CHUNK):
                digest.update(chunk)
                size += len(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        final = directory / digest.hexdigest()
        if final.exists():
            os.unlink(temporary)
        else:
            os.replace(temporary, final)
            os.chmod(final, 0o440)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    return Received(digest.hexdigest(), size, final)


def submit_artifact(
    miroir: ResolvedMiroir,
    connection: ResolvedConnection,
    source: Path,
    options: dict[str, Any] | None = None,
    *,
    store: MetadataStore,
    root: Path,
    original_name: str | None = None,
    origin: ArtifactOrigin = "upload",
    input_format: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Validate, store, and register one file with its import options.

    Every transport (upload, receiving URL, watched folder) comes through here.
    Invalid files or options are rejected before anything is stored or recorded.
    """
    try:
        file_format = connection.integration.format(input_format)
    except KeyError:
        raise OperationError("This integration does not accept that file") from None
    try:
        chosen = file_format.options.model_validate(options or {})
    except ValidationError as error:
        fields = ", ".join(
            ".".join(map(str, issue["loc"])) or "options"
            for issue in error.errors(include_input=False)
        )
        raise OperationError(f"Invalid import options: {fields}") from None
    file_format.validate(source)
    received = receive_file(source, root, miroir.id, connection.config.id)
    return store.register_artifact(
        miroir_id=miroir.id,
        connection_id=connection.config.id,
        sha256=received.sha256,
        size=received.size,
        input_format=file_format.id,
        options=json.dumps(chosen.model_dump(mode="json"), sort_keys=True),
        origin=origin,
        original_name=(original_name or source.name)[:255],
        storage_path=str(received.path),
        # Folder copies rotate (the folder keeps the original); others are kept.
        retention="rolling" if origin == "folder" else "keep",
    )


def stored_copy_is_intact(artifact: dict[str, Any]) -> bool:
    path = Path(artifact["storage_path"])
    if not path.is_file():
        return False
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while chunk := reader.read(CHUNK):
            digest.update(chunk)
    return bool(digest.hexdigest() == artifact["sha256"])
