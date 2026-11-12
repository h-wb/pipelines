"""Safe connector diagnostics shared by checks and extraction."""

from typing import Any, Literal

ErrorKind = Literal["credentials_rejected", "rate_limited", "unexpected_response"]


class IntegrationError(Exception):
    def __init__(self, message: str, kind: ErrorKind) -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def rejected(self) -> bool:
        return self.kind == "credentials_rejected"


def is_rate_limited(response: Any) -> bool:
    headers = getattr(response, "headers", None) or {}
    return getattr(response, "status_code", None) == 429 or (
        getattr(response, "status_code", None) == 403
        and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)
    )


def response_object(response: Any, service: str) -> dict[str, Any]:
    """Validate without copying remote bodies or request URLs into diagnostics."""
    if is_rate_limited(response):
        raise IntegrationError(
            f"{service} rate limit reached; try again later", "rate_limited"
        )
    if response.status_code in (401, 403):
        raise IntegrationError(
            f"{service} rejected access; check the token and its permissions",
            "credentials_rejected",
        )
    if not 200 <= response.status_code < 300:
        # Keep the original HTTP exception for retry and failure classification.
        response.raise_for_status()
    try:
        body = response.json()
    except ValueError as error:
        raise IntegrationError(
            f"{service} returned an unexpected response", "unexpected_response"
        ) from error
    if not isinstance(body, dict):
        raise IntegrationError(
            f"{service} returned an unexpected response", "unexpected_response"
        )
    return body
