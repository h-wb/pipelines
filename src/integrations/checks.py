"""Connection checks: confirm settings and credentials work, without loading data.

A check raises `CheckFailed` with a message that is safe to show. Credential
values never appear in it. `rejected=True` means the service refused the
credentials, as opposed to being unreachable.
"""

import os
from typing import Any


class CheckFailed(Exception):
    def __init__(self, message: str, rejected: bool = False):
        super().__init__(message)
        self.rejected = rejected


def listenbrainz(settings: Any, credentials: dict[str, str]) -> str:
    import requests

    try:
        response = requests.get(
            "https://api.listenbrainz.org/1/validate-token",
            headers={"Authorization": f"Token {credentials['access_token']}"},
            timeout=20,
        )
    except requests.RequestException as error:
        raise CheckFailed(
            f"ListenBrainz unreachable ({type(error).__name__})"
        ) from None
    if response.status_code in (401, 403):
        raise CheckFailed("ListenBrainz rejected the token", rejected=True)
    if response.status_code != 200:
        raise CheckFailed(f"ListenBrainz answered HTTP {response.status_code}")
    body = response.json()
    if not body.get("valid"):
        raise CheckFailed("ListenBrainz says the token is not valid", rejected=True)
    if body.get("user_name") != settings.username:
        raise CheckFailed(
            "The token belongs to a different ListenBrainz account than "
            f"'{settings.username}'",
            rejected=True,
        )
    return f"Token valid for {settings.username}"


def apple_health(settings: Any, credentials: dict[str, str]) -> str:
    if settings.data_dir is None:
        return "Ready to receive Health Auto Export JSON; upload an export to verify it"
    if not os.path.isdir(settings.data_dir):
        raise CheckFailed("Export folder does not exist on this machine")
    if not os.access(settings.data_dir, os.R_OK | os.X_OK):
        raise CheckFailed("Export folder is not readable")
    return "Export folder is readable"
