"""File capabilities derived from connector declarations; no second registration list."""

from src.integrations.registry import INTEGRATIONS
from src.integrations.validation import InvalidArtifact

__all__ = ["INPUTS", "PARSER_VERSIONS", "VALIDATORS", "InvalidArtifact"]

INPUTS = {i.file_input.id: i.file_input for i in INTEGRATIONS.values() if i.file_input}
VALIDATORS = {name: definition.validate for name, definition in INPUTS.items()}
PARSER_VERSIONS = {
    name: definition.parser_version for name, definition in INPUTS.items()
}
