"""Declarative activity feed; heterogeneous payloads stay in one JSON column."""

from dlt.sources.rest_api import EndpointResource


def events(username: str) -> EndpointResource:
    return {
        "name": "events",
        "primary_key": "id",
        "columns": {"payload": {"data_type": "json"}},
        "endpoint": {"path": f"/users/{username}/events"},
    }
