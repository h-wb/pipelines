"""GitHub contributions daily resource."""

from typing import Any

import dlt
from dlt.common.pendulum import pendulum
from dlt.common.typing import TDataItems
from dlt.sources import incremental

from src.integrations.github.client import GitHubClient, parse_datetime
from src.integrations.github.incremental import INITIAL_DATE, resume_cursor


@dlt.resource(write_disposition="merge", primary_key="date")
def contributions_daily(
    api: GitHubClient,
    date: incremental[str] = incremental("date", initial_value=INITIAL_DATE, lag=3),  # noqa: B008
) -> TDataItems:
    start = max(
        parse_datetime(resume_cursor(date, api.cursor_starts, "last_date")),
        api.account_created,
    )
    now = pendulum.now("UTC")
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar { weeks { contributionDays { date contributionCount } } }
        }
      }
    }"""
    days: list[dict[str, Any]] = []
    # contributionsCollection spans at most one year per query
    while start < now:
        end = min(start.add(years=1).subtract(seconds=1), now)
        response = api.authenticated_client.post(
            "/graphql",
            json={
                "query": query,
                "variables": {
                    "login": api.username,
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                },
            },
        )
        response.raise_for_status()
        body = response.json()
        if "errors" in body:
            raise RuntimeError(f"GitHub GraphQL error: {body['errors']}")
        for week in body["data"]["user"]["contributionsCollection"][
            "contributionCalendar"
        ]["weeks"]:
            days += week["contributionDays"]
        start = end.add(seconds=1)
    yield days
