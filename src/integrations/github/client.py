"""Shared GitHub requests and lazy, per-source account/repository discovery."""

import time
from collections.abc import Iterator
from functools import cached_property
from typing import Any

import dlt
from dlt.common.configuration import configspec
from dlt.common.pendulum import pendulum
from dlt.extract import DltResource
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth
from dlt.sources.helpers.rest_client.client import RESTClient
from dlt.sources.helpers.rest_client.paginators import HeaderLinkPaginator
from dlt.sources.rest_api import EndpointResource, rest_api_resources
from pendulum import DateTime
from requests import HTTPError, PreparedRequest, Session

from src.integrations.errors import (
    IntegrationError,
    response_object,
)
from src.integrations.github.account import verify_account

API_URL = "https://api.github.com"
SEARCH_CAP = 1000
SEARCH_PAUSE_S = 2.1
OVERLAP = pendulum.duration(days=3)


def iso(dt: DateTime) -> str:
    return str(dt.in_timezone("UTC").format("YYYY-MM-DD[T]HH:mm:ss[Z]"))


def parse_datetime(value: str) -> DateTime:
    parsed = pendulum.parse(value)
    if not isinstance(parsed, DateTime):
        raise TypeError("Expected a datetime")
    return parsed


class GitHubClient:
    def __init__(self, username: str, access_token: str, session: Session) -> None:
        self.username = username
        self.external_query = f"author:{username} -user:{username}"
        self.cursor_starts: dict[str, str] = {}
        self._client = RESTClient(
            base_url=API_URL,
            session=session,
            auth=BearerTokenAuth(access_token),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            paginator=HeaderLinkPaginator(),
        )

    def rest_resources(self, *definitions: EndpointResource) -> list[DltResource]:
        return rest_api_resources(
            {
                "client": {
                    "base_url": API_URL,
                    "session": self._client.session,
                    "auth": VerifiedAccountAuth(self),
                    "headers": self._client.headers,
                    "paginator": HeaderLinkPaginator(),
                },
                "resource_defaults": {
                    "write_disposition": "merge",
                    "endpoint": {"params": {"per_page": 100}, "data_selector": "$"},
                },
                "resources": list(definitions),
            }
        )

    @cached_property
    def account_created(self) -> DateTime:
        response = self._client.get("/user")
        body = response_object(response, "GitHub")
        verify_account(body, self.username)
        try:
            created = parse_datetime(body["created_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrationError(
                "GitHub returned an unexpected account creation date",
                "unexpected_response",
            ) from error
        return created

    @cached_property
    def authenticated_client(self) -> RESTClient:
        # Every selected resource verifies the account, even without a date cursor.
        _ = self.account_created
        return self._client

    def since(
        self, state_key: str = "last_run", first: DateTime | None = None
    ) -> DateTime:
        last = dlt.current.resource_state().get(state_key)
        return (
            parse_datetime(last) - OVERLAP if last else (first or self.account_created)
        )

    def mark(self, at: DateTime, state_key: str = "last_run") -> None:
        dlt.current.resource_state()[state_key] = at.isoformat()

    @cached_property
    def owned_repos(self) -> list[str]:
        return [
            r["full_name"]
            for page in self.authenticated_client.paginate(
                "/user/repos", params={"per_page": 100, "affiliation": "owner"}
            )
            for r in page
        ]

    def pages(self, path: str, **kwargs: Any) -> Iterator[Any]:
        """A repository or branch may disappear after it was discovered."""
        try:
            yield from self.authenticated_client.paginate(path, **kwargs)
        except HTTPError as error:
            if error.response is None or error.response.status_code != 404:
                raise

    def commit_with_stats(self, repo: str, sha: str) -> dict[str, Any] | None:
        response = self.authenticated_client.get(f"/repos/{repo}/commits/{sha}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        commit: dict[str, Any] = response.json()
        commit.pop("files", None)
        return commit

    def search(
        self, path: str, query: str, qualifier: str, start: DateTime, end: DateTime
    ) -> Iterator[dict]:
        """Split date windows when GitHub's search result cap is reached."""
        ranges: list[tuple[DateTime, DateTime]] = [(start, end)]
        while ranges:
            lo, hi = ranges.pop()
            params = {
                "q": f"{query} {qualifier}:{iso(lo)}..{iso(hi)}",
                "per_page": 100,
            }
            first = True
            for page in self.authenticated_client.paginate(
                path, params=params, data_selector="$"
            ):
                time.sleep(SEARCH_PAUSE_S)
                if first:
                    first = False
                    if page[0][
                        "total_count"
                    ] > SEARCH_CAP and hi - lo > pendulum.duration(hours=1):
                        mid = lo + (hi - lo) / 2
                        ranges += [(lo, mid), (mid, hi)]
                        break
                yield from page[0]["items"]


@configspec
class VerifiedAccountAuth(BearerTokenAuth):
    """Keep declarative requests lazy while enforcing the same account guard."""

    def __init__(self, api: GitHubClient) -> None:
        auth = api._client.auth
        assert isinstance(auth, BearerTokenAuth)
        super().__init__(token=auth.token)
        self.api = api

    def __call__(self, request: PreparedRequest) -> PreparedRequest:
        _ = self.api.account_created
        return super().__call__(request)
