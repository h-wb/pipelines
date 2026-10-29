"""Delivery descriptions shared by onboarding and execution readiness.

Integrations declare how data can arrive. Transports own HTTP/forms, sources own
parsing, and these descriptions contain neither storage nor orchestration code.
"""

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class DeliveryMethod:
    id: Literal["api", "upload", "webhook", "folder"]
    label: str
    description: str
    settings_fields: tuple[str, ...] = ()
    advanced: bool = False

    def can_sync(self, settings: Any) -> bool:
        return self.id in ("api", "folder") and all(
            getattr(settings, name, None) for name in self.settings_fields
        )


@dataclass(frozen=True)
class FileDelivery:
    label: str
    accept: str
    content_type: str
    sender_instructions: str
    role_help: str
