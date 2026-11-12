"""Declarative GitHub repository stream (broader than owned-only discovery)."""

from dlt.sources.rest_api import EndpointResource

REPOS: EndpointResource = {
    "name": "repos",
    "primary_key": "id",
    "endpoint": {
        "path": "/user/repos",
        "params": {"affiliation": "owner,collaborator,organization_member"},
    },
}
