"""Explicit bundled catalog. Importing it performs no I/O or runtime imports."""

from types import MappingProxyType

from src.integrations.apple_health import DEFINITION as APPLE_HEALTH
from src.integrations.bikeshare import DEFINITION as BIKESHARE
from src.integrations.browser_history import DEFINITION as BROWSER_HISTORY
from src.integrations.github import DEFINITION as GITHUB
from src.integrations.listenbrainz import DEFINITION as LISTENBRAINZ

INTEGRATIONS = MappingProxyType(
    {
        i.name: i
        for i in (LISTENBRAINZ, APPLE_HEALTH, GITHUB, BIKESHARE, BROWSER_HISTORY)
    }
)
