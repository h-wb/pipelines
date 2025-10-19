"""Bike Share Toronto data source.

Extracts trip data from the Bike Share Toronto mobile app's API.
"""

from typing import Iterable
import urllib3
import requests
import dlt
from dlt.common.typing import TDataItems
from dlt.extract import DltResource
from dlt.sources.helpers.rest_client.client import RESTClient
from dlt.sources.helpers.rest_client.auth import APIKeyAuth


@dlt.source
def bikeshare(
    member_id: str = dlt.secrets.value, authorization_token: str = dlt.secrets.value
) -> Iterable[DltResource]:
    """
    Bike Share Toronto trips source.

    Args:
        member_id: Bike Share member ID
        authorization_token: Auth token from mobile app

    Returns:
        dlt resource with trip data
    """
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.verify = False

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

    @dlt.resource(write_disposition="append", primary_key="rentalId")
    def trips(
        sd: dlt.sources.incremental[int] = dlt.sources.incremental(
            "sd", initial_value=0
        ),
    ) -> TDataItems:
        page_size = 500 if sd.last_value == sd.initial_value else 50

        response = client.get(
            "rental/closed",
            params={
                "memberId": member_id,
                "isBikeAngel": "false",
                "pageSize": page_size,
                "pageOffset": 0,
            },
        )

        data = response.json()
        rentals = data.get("rentals", {})
        trips_data = next(iter(rentals.values()), [])

        yield trips_data

    return trips
