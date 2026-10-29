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


def github(settings: Any, credentials: dict[str, str]) -> str:
    import requests

    try:
        response = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {credentials['access_token']}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
        )
        if response.status_code in (401, 403):
            raise CheckFailed(
                "GitHub rejected access; check the token and its permissions",
                rejected=True,
            )
        if response.status_code != 200:
            raise CheckFailed(f"GitHub answered HTTP {response.status_code}")
        if response.json().get("login", "").casefold() != settings.username.casefold():
            raise CheckFailed(
                "The GitHub token belongs to a different account", rejected=True
            )
    except requests.RequestException as error:
        raise CheckFailed(f"GitHub unreachable ({type(error).__name__})") from None
    return "GitHub account access verified"


def bikeshare(settings: Any, credentials: dict[str, str]) -> str:
    import requests

    try:
        response = requests.get(
            "https://layer.bicyclesharing.net/mobile/v1/tor/rental/closed",
            headers={
                "Authorization": credentials["authorization_token"],
                "Accept": "application/json",
                "User-Agent": "Bike%20Share/2025.33.3.26433108 CFNetwork/3860.100.1 Darwin/25.0.0",
            },
            params={
                "memberId": credentials["member_id"],
                "isBikeAngel": "false",
                "pageSize": "1",
                "pageOffset": "0",
            },
            verify=settings.verify_tls,
            timeout=20,
        )
        if response.status_code in (401, 403):
            raise CheckFailed(
                "Bike Share rejected the mobile-app credentials", rejected=True
            )
        if response.status_code != 200:
            raise CheckFailed(f"Bike Share answered HTTP {response.status_code}")
        if "rentals" not in response.json():
            raise CheckFailed("Bike Share returned an unexpected response")
    except requests.RequestException as error:
        raise CheckFailed(
            f"Bike Share unreachable ({type(error).__name__}); check certificate settings if needed"
        ) from None
    return "Bike Share account access verified"


def browser_history(settings: Any, credentials: dict[str, str]) -> str:
    if settings.data_dir is None:
        return "Ready to receive Firefox/Zen snapshots; upload one to verify it"
    from pathlib import Path

    from src.integrations.browser_history import validate_snapshot
    from src.integrations.validation import InvalidArtifact

    try:
        validate_snapshot(Path(settings.data_dir) / "zen-places.sqlite.gz")
    except (OSError, InvalidArtifact):
        raise CheckFailed(
            "No valid, readable zen-places.sqlite.gz snapshot in this folder"
        ) from None
    return "Browser snapshot is readable and valid"
