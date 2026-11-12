"""Account identity validation shared by the lightweight check and dlt client."""

from typing import Any

from src.integrations.errors import IntegrationError


def verify_account(body: dict[str, Any], username: str) -> None:
    login = body.get("login")
    if not isinstance(login, str) or not login:
        raise IntegrationError(
            "GitHub returned an unexpected account response", "unexpected_response"
        )
    if login.casefold() != username.casefold():
        raise IntegrationError(
            "The GitHub token belongs to a different account", "credentials_rejected"
        )
