"""ListenBrainz data source implementation."""

import dlt
from dlt.common.pendulum import pendulum
from dlt.sources.rest_api import rest_api_source
from dlt.sources.helpers.rest_client.paginators import BasePaginator

PAGE_SIZE = 1000


class ListenBrainzPaginator(BasePaginator):
    """Page forward through listens by `min_ts` without dropping same-second ties.

    ListenBrainz's `min_ts` is exclusive, so paging from the newest timestamp of a
    page skips any other listen in that same second. Always ask for one second
    earlier; the re-fetched boundary rows are deduped by the merge primary key.
    """

    def init_request(self, request) -> None:
        # also covers the incremental resume point / initial start_date
        self._min_ts = int(request.params["min_ts"]) - 1
        request.params["min_ts"] = self._min_ts

    def update_state(self, response, data=None) -> None:
        listens = response.json()["payload"]["listens"]
        if len(listens) < PAGE_SIZE:
            self._has_next_page = False
            return
        next_min_ts = max(listen["listened_at"] for listen in listens) - 1
        if next_min_ts < self._min_ts:
            raise RuntimeError(f"ListenBrainz page did not advance past min_ts={self._min_ts}")
        self._min_ts = next_min_ts

    def update_request(self, request) -> None:
        request.params["min_ts"] = self._min_ts


@dlt.source(name="listenbrainz")
def listenbrainz_source(
    username: str = dlt.config.value,
    access_token: str = dlt.secrets.value,
    start_date: str = dlt.config.value,
) -> dlt.sources.DltResource:
    """
    ListenBrainz data source for extracting listening history.

    Args:
        username: ListenBrainz username
        access_token: ListenBrainz API token
        start_date: Start date for initial data load

    Returns:
        DltResource: A resource with listen events
    """
    source = rest_api_source(
        {
            "client": {
                "base_url": "https://api.listenbrainz.org",
                "auth": {
                    "type": "bearer",
                    "token": access_token,
                },
            },
            "resources": [
                {
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
                            "initial_value": pendulum.parse(start_date).int_timestamp,
                        },
                    },
                },
            ],
        }
    )

    return source
