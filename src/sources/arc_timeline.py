"""Arc Timeline data source implementation."""

import dlt
from dlt.common.pendulum import pendulum
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
        code = input("Code: ")
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

    def _read_folder_json(folder: DriveNode):
        """Read all JSON files from a folder and yield their contents."""
        try:
            for item in folder.dir():
                print(f"  Processing {item}")
                try:
                    with folder[item].open(stream=True) as response:
                        data = response.json()
                        for record in data:
                            yield record
                except Exception as e:
                    print(f"  Error reading {item}: {e}")
        except KeyError:
            print(f"  {folder.name} folder not found")

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
        yield from _read_folder_json(exports_data["export"]["items"])

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
        yield from _read_folder_json(exports_data["export"]["samples"])

    @dlt.resource(
        name="places",
        data_from=exports,
        primary_key="id",
        write_disposition="merge",
        parallelized=True,
    )
    def places(exports_data):
        """Extract place data from export."""
        yield from _read_folder_json(exports_data["export"]["places"])

    return metadata, items, samples, places
