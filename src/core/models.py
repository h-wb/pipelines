"""Common validation rules for persisted miroir configuration."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,23}$")]
DatabaseIdentifier = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,62}$")
]
SecretKey = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True
    )
