"""Connection checks: confirm settings and credentials work, without loading data.

A check raises `CheckFailed` with a message that is safe to show. Credential
values never appear in it. `rejected=True` means the service refused the
credentials, as opposed to being unreachable.
"""

from typing import Any

from src.integrations.errors import (
    IntegrationError,
    response_object,
)


class CheckFailed(Exception):
    def __init__(self, message: str, rejected: bool = False):
        super().__init__(message)
        self.rejected = rejected


def _body(response: Any, service: str) -> dict[str, Any]:
    """The JSON object answered; anything else (e.g. an HTML bot check) fails."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise CheckFailed(f"{service} returned an unexpected response")
    return body


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
    body = _body(response, "ListenBrainz")
    if not body.get("valid"):
        raise CheckFailed("ListenBrainz says the token is not valid", rejected=True)
    if body.get("user_name") != settings.username:
        raise CheckFailed(
            "The token belongs to a different ListenBrainz account than "
            f"'{settings.username}'",
            rejected=True,
        )
    return f"Token valid for {settings.username}"


def github(settings: Any, credentials: dict[str, str]) -> str:
    import requests

    from src.integrations.github.account import verify_account

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
        if response.status_code not in (200, 401, 403, 429):
            raise CheckFailed(f"GitHub answered HTTP {response.status_code}")
        verify_account(response_object(response, "GitHub"), settings.username)
    except IntegrationError as error:
        raise CheckFailed(str(error), rejected=error.rejected) from error
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
        if "rentals" not in _body(response, "Bike Share"):
            raise CheckFailed("Bike Share returned an unexpected response")
    except requests.RequestException as error:
        raise CheckFailed(
            f"Bike Share unreachable ({type(error).__name__}); check certificate settings if needed"
        ) from None
    return "Bike Share account access verified"
