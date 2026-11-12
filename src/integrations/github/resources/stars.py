"""Declarative stars stream; merge retains previously observed stars."""

from dlt.sources.rest_api import EndpointResource

STARS: EndpointResource = {
    "name": "stars",
    "primary_key": "repo__id",
    "endpoint": {
        "path": "/user/starred",
        "headers": {"Accept": "application/vnd.github.star+json"},
    },
}
