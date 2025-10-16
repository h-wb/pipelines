"""Arc Timeline data source implementation."""

import dlt
from dlt.common.pendulum import pendulum
from dlt.sources import TDataItem
from prefect import pause_flow_run
from pyicloud import PyiCloudService
from pyicloud.services.drive import DriveNode


@dlt.source(name="arc_timeline")
def arc_timeline_source(
    apple_id: str = dlt.config.value,
    password: str = dlt.secrets.value,
):
    """Arc Timeline source for extracting Arc Editor export data from iCloud Drive."""
    print(f"Authenticating with iCloud as {apple_id}...")
    api = PyiCloudService(apple_id, password)

    if api.requires_2fa:
        print("Two-factor authentication required.")
        print("Enter the code you received on one of your approved devices:")
        code = pause_flow_run(wait_for_input=str)
        result = api.validate_2fa_code(code)

        if not result:
            raise Exception("2FA validation failed")

        if not api.is_trusted_session:
            print("Session is not trusted. Requesting trust...")
            api.trust_session()

    print("Successfully authenticated with iCloud!")

    @dlt.resource(
        name="exports",
        primary_key="export_date",
        write_disposition="append",
    )
    def exports(
        export_date: dlt.sources.incremental[
            pendulum.DateTime
        ] = dlt.sources.incremental(
            "export_date",
            initial_value=pendulum.parse("2020-01-01"),
        ),
    ):
        """Discover and track export folders using date_modified for incremental loading."""

        def parse_export_date(folder):
            return pendulum.from_format(folder[7:], "YYYY-MM-DD-HHmmss")

        exports_folder = api.drive["Arc Editor"]["Exports"]
        latest_folder = max(exports_folder.dir(), key=parse_export_date)
        latest_date = parse_export_date(latest_folder)

        if latest_folder and latest_date > export_date.last_value:
            print(f"Found new export: {latest_folder} (export date: {latest_date})")
            yield {"export": exports_folder[latest_folder], "export_date": latest_date}
        else:
            print(f"No new exports. Last processed: {export_date.last_value}")

    @dlt.resource(name="metadata", data_from=exports, write_disposition="append")
    def metadata(exports_data):
        """Read and yield metadata.json content from export."""
        try:
            with exports_data["export"]["metadata.json"].open(stream=True) as response:
                yield response.json()
        except KeyError:
            print("metadata.json not found")

    @dlt.defer
    def _read_file(item: DriveNode) -> TDataItem:
        print(f"  Processing {item.name}")
        try:
            with item.open(stream=True) as response:
                return response.json()
        except Exception as e:
            print(f"  Error reading {item}: {e}")

    @dlt.resource(
        name="items",
        data_from=exports,
        primary_key="base__id",
        write_disposition="merge",
        parallelized=True,
        references=[
            {
                "referenced_table": "samples",
                "columns": ["id"],
                "referenced_columns": ["visit__placeId"],
            }
        ],
    )
    def items(exports_data):
        """Extract base timeline item data from export."""
        files = exports_data["export"]["items"]
        for file in files.dir():
            yield _read_file(files[file])

    @dlt.resource(
        name="samples",
        data_from=exports,
        primary_key="id",
        write_disposition="merge",
        parallelized=True,
        references=[
            {
                "referenced_table": "items",
                "columns": ["id"],
                "referenced_columns": ["timelineItemId"],
            }
        ],
    )
    def samples(exports_data):
        """Extract sample data with reference to timeline_items via timelineItemId."""
        files = exports_data["export"]["samples"]
        for file in files.dir():
            yield _read_file(files[file])

    @dlt.resource(
        name="places",
        data_from=exports,
        primary_key="id",
        write_disposition="merge",
        parallelized=True,
    )
    def places(exports_data):
        """Extract place data from export."""
        files = exports_data["export"]["places"]
        for file in files.dir():
            yield _read_file(files[file])

    return metadata, items, samples, places
