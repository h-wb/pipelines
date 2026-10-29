"""Setup choices shown during onboarding, derived from a connector's inputs.

They describe how data can arrive; transports own HTTP/forms/folders, formats
own parsing, and these descriptions contain neither storage nor orchestration.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class DeliveryMethod:
    id: Literal["api", "upload", "webhook", "folder"]
    label: str
    description: str
    advanced: bool = False
