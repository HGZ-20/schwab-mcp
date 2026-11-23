"""Exception hierarchy for schwab-mcp."""

from __future__ import annotations

from typing import Any


class SchwabMCPError(RuntimeError):
    """Base exception for all schwab-mcp errors."""


class SchwabAuthError(SchwabMCPError):
    """Raised when Schwab OAuth credentials, token loading, or token refresh fails."""

    def __init__(self, message: str, *, auth_required: bool = True) -> None:
        super().__init__(message)
        self.auth_required = auth_required


class SchwabAPIError(SchwabMCPError):
    """Raised when a Charles Schwab REST API endpoint returns an HTTP error."""

    def __init__(
        self,
        status_code: int,
        endpoint: str,
        message: str,
        raw_body: Any = None,
    ) -> None:
        super().__init__(f"Schwab API error {status_code} on {endpoint}: {message}")
        self.status_code = status_code
        self.endpoint = endpoint
        self.message = message
        self.raw_body = raw_body


class RiskBlockedError(SchwabMCPError):
    """Raised when an order or cancellation violates configured RiskGuard policies."""
