"""Bike Share Toronto data source.

Extracts trip data from the Bike Share Toronto mobile app's API.
"""

from collections.abc import Iterable

import dlt
import requests
from dlt.common.typing import TDataItems
from dlt.extract import DltResource
from dlt.sources.helpers.rest_client.auth import APIKeyAuth
from dlt.sources.helpers.rest_client.client import RESTClient


@dlt.source
def bikeshare(
    member_id: str, authorization_token: str, session: requests.Session
) -> Iterable[DltResource]:
    """
    Bike Share Toronto trips source.

    Args:
        member_id: Bike Share member ID
        authorization_token: Auth token from mobile app

    Returns:
        dlt resource with trip data
    """

    client = RESTClient(
        base_url="https://layer.bicyclesharing.net/mobile/v1/tor",
        auth=APIKeyAuth(
            name="Authorization", api_key=authorization_token, location="header"
        ),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Bike%20Share/2025.33.3.26433108 CFNetwork/3860.100.1 Darwin/25.0.0",
            "api-key": "",
            "X-ba-api-key": "",
            "Accept-Language": "en",
            "Cache-Control": "no-cache",
        },
        session=session,
    )

    @dlt.resource(write_disposition="merge", primary_key="rentalId")
    def trips(
        sd: dlt.sources.incremental[int] = dlt.sources.incremental(  # noqa: B008
            "sd", initial_value=0
        ),
    ) -> TDataItems:
        page_size = 500 if sd.last_value == sd.initial_value else 50

        offset = 0
        seen: set[str] = set()
        try:
            while True:
                response = client.get(
                    "rental/closed",
                    params={
                        "memberId": member_id,
                        "isBikeAngel": "false",
                        "pageSize": page_size,
                        "pageOffset": offset,
                    },
                )
                response.raise_for_status()
                rentals = response.json().get("rentals", {})
                pages = (
                    list(rentals.values()) if isinstance(rentals, dict) else [rentals]
                )
                trips_data = [trip for group in pages for trip in group]
                if not trips_data:
                    break
                ids = {str(trip["rentalId"]) for trip in trips_data}
                if ids <= seen:
                    raise ValueError(
                        "Bike Share repeated a page; history may be incomplete"
                    )
                seen.update(ids)
                yield trips_data
                if len(trips_data) < page_size:
                    break
                # Do not assume all records in an API page are sorted. Stop only
                # after reading a whole page older than the committed boundary.
                if sd.last_value and all(
                    trip["sd"] < sd.last_value for trip in trips_data
                ):
                    break
                offset += page_size
        finally:
            session.close()

    return trips
