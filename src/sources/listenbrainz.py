"""ListenBrainz data source implementation."""

import dlt
from dlt.common.pendulum import pendulum
from dlt.sources.rest_api import rest_api_source
from dlt.sources.helpers.rest_client.paginators import JSONResponseCursorPaginator


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
                    "write_disposition": "append",
                    "primary_key": "listened_at",
                    "endpoint": {
                        "path": f"/1/user/{username}/listens",
                        "params": {
                            "count": 1000,
                            "min_ts": "{incremental.last_value}",
                        },
                        "paginator": JSONResponseCursorPaginator(
                            cursor_path="payload.listens[0].listened_at",
                            cursor_param="min_ts",
                        ),
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
