"""The error every file-format validator raises. Stdlib only.

Validation checks structure, not every record: the parser still fails loudly on
records it cannot interpret, and that failure is recorded on the import attempt.
"""


class InvalidArtifact(ValueError):
    """The file is not in the expected format. Never echoes contents."""
