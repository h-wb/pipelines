"""ListenBrainz data source implementation."""

import time
from datetime import datetime
from typing import Any, cast

import dlt
import requests
from dlt.extract import DltSource
from dlt.sources.helpers.rest_client.paginators import BasePaginator
from dlt.sources.rest_api import EndpointResource, RESTAPIConfig, rest_api_source

PAGE_SIZE = 1000
# ListenBrainz serves an HTML bot check (HTTP 200) to clients paging too fast,
# which breaks full refreshes; ~124 pages at this pace is ~3 minutes
PAGE_PAUSE_S = 1.5


class ListenBrainzPaginator(BasePaginator):
    """Page forward through listens by `min_ts` without dropping same-second ties.

    ListenBrainz's `min_ts` is exclusive, so paging from the newest timestamp of a
    page skips any other listen in that same second. Always ask for one second
    earlier; the re-fetched boundary rows are deduped by the merge primary key.
    """

    def init_request(self, request: requests.Request) -> None:
        # also covers the incremental resume point / initial start_date
        params = cast(dict[str, Any], request.params)
        self._min_ts = int(params["min_ts"]) - 1
        params["min_ts"] = self._min_ts

    def update_state(
        self, response: requests.Response, data: list[Any] | None = None
    ) -> None:
        # Fail on HTTP errors (e.g. 401 for a revoked token) before reading the body,
        # so the cause is an HTTPError rather than a KeyError on the error payload.
        response.raise_for_status()
        listens = response.json()["payload"]["listens"]
        if len(listens) < PAGE_SIZE:
            self._has_next_page = False
            return
        next_min_ts = max(listen["listened_at"] for listen in listens) - 1
        if next_min_ts < self._min_ts:
            raise RuntimeError(
                f"ListenBrainz page did not advance past min_ts={self._min_ts}"
            )
        self._min_ts = next_min_ts

    def update_request(self, request: requests.Request) -> None:
        time.sleep(PAGE_PAUSE_S)
        cast(dict[str, Any], request.params)["min_ts"] = self._min_ts


@dlt.source(name="listenbrainz")
def listenbrainz_source(
    username: str,
    access_token: str,
    start_date: str,
    session: Any,
) -> DltSource:
    """
    ListenBrainz data source for extracting listening history.

    Args:
        username: ListenBrainz username
        access_token: ListenBrainz API token
        start_date: Start date for initial data load

    Returns:
        DltSource: the listens resource
    """
    listens: EndpointResource = {
        "name": "listens",
        # merge (not append) so re-fetched windows update instead of duplicating;
        # listened_at alone isn't unique (two listens can share a second)
        "write_disposition": "merge",
        "primary_key": ["listened_at", "recording_msid"],
        "endpoint": {
            "path": f"/1/user/{username}/listens",
            "params": {
                "count": PAGE_SIZE,
                "min_ts": "{incremental.last_value}",
            },
            "paginator": ListenBrainzPaginator(),
            "data_selector": "payload.listens",
            "incremental": {
                "cursor_path": "listened_at",
                # Settings validation normalizes start_date to an offset-aware ISO form.
                "initial_value": int(datetime.fromisoformat(start_date).timestamp()),
            },
        },
    }
    config: RESTAPIConfig = {
        "client": {
            "base_url": "https://api.listenbrainz.org",
            "session": session,
            "auth": {
                "type": "bearer",
                "token": access_token,
            },
        },
        "resources": [listens],
    }
    return rest_api_source(config)
