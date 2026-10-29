"""Receive submitted files into durable, content-addressed artifact storage.

Files are copied to a temporary name, flushed to disk, then renamed into place, so
an interrupted copy never appears under an artifact's name. Storage is local for
now; the stored path is recorded so another backend can replace it later.
"""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.config import ResolvedConnection, ResolvedMiroir
from src.core.operations import MetadataStore, OperationError, Role
from src.integrations.formats import VALIDATORS

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
    role: Role,
    *,
    store: MetadataStore,
    root: Path,
    original_name: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Validate, store, and register one file. Does not import it.

    Invalid files are rejected before anything is stored or recorded.
    """
    input_format = connection.integration.input_format
    if (
        input_format is None
        or "artifact_import" not in connection.integration.capabilities
    ):
        raise OperationError("Integration does not accept files")
    delivery = connection.integration.file_delivery
    if delivery is not None and role not in delivery.roles:
        raise OperationError(
            f"{delivery.label} files can only be imported as: {', '.join(delivery.roles)}"
        )
    VALIDATORS[input_format](source)
    received = receive_file(source, root, miroir.id, connection.config.id)
    return store.register_artifact(
        miroir_id=miroir.id,
        connection_id=connection.config.id,
        sha256=received.sha256,
        size=received.size,
        input_format=input_format,
        role=role,
        original_name=(original_name or source.name)[:255],
        storage_path=str(received.path),
        retention="keep",
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
