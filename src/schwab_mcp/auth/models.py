"""Pydantic models for Schwab OAuth 2.0 tokens and MCP OAuth 2.1 state."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

# Charles Schwab enforces a 30-minute access token and a hard 7-day refresh token expiration.
DEFAULT_ACCESS_LIFETIME_SEC = 1800
REFRESH_TOKEN_LIFETIME_SEC = 7 * 24 * 3600
ACCESS_REFRESH_BUFFER_SEC = 180


class TokenData(BaseModel):
    """Persisted Charles Schwab OAuth 2.0 token payload with expiration timestamps."""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    scope: str = "api"
    expires_in: int = DEFAULT_ACCESS_LIFETIME_SEC
    id_token: str | None = None
    obtained_at: float
    access_expires_at: float
    refresh_obtained_at: float
    refresh_expires_at: float

    def access_seconds_remaining(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return self.access_expires_at - now

    def refresh_seconds_remaining(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return self.refresh_expires_at - now

    def needs_access_refresh(
        self,
        now: float | None = None,
        buffer_sec: int = ACCESS_REFRESH_BUFFER_SEC,
    ) -> bool:
        return self.access_seconds_remaining(now) <= buffer_sec


class PendingUpstreamAuth(BaseModel):
    """Pending MCP client authorization request waiting for Schwab OAuth callback."""

    upstream_state: str
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    client_state: str | None = None
    scopes: list[str] = Field(default_factory=list)
    resource: str | None = None
    expires_at: float


class StoredAuthCode(BaseModel):
    """Persisted MCP OAuth 2.1 authorization code bound to a Schwab subject."""

    code: str
    client_id: str
    scopes: list[str] = Field(default_factory=list)
    expires_at: float
    code_challenge: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    resource: str | None = None
    subject: str


class StoredAccessToken(BaseModel):
    """Persisted MCP OAuth 2.1 access token bound to a Schwab subject."""

    token: str
    client_id: str
    scopes: list[str] = Field(default_factory=list)
    expires_at: int
    resource: str | None = None
    subject: str
    refresh_token_id: str | None = None


class StoredRefreshToken(BaseModel):
    """Persisted MCP OAuth 2.1 refresh token bound to a Schwab subject."""

    token: str
    client_id: str
    scopes: list[str] = Field(default_factory=list)
    expires_at: int
    resource: str | None = None
    subject: str
    access_token_id: str | None = None


class OAuthProviderState(BaseModel):
    """Persistent state for the MCP OAuth 2.1 Authorization Server."""

    version: int = 1
    clients: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pending: dict[str, PendingUpstreamAuth] = Field(default_factory=dict)
    auth_codes: dict[str, StoredAuthCode] = Field(default_factory=dict)
    access_tokens: dict[str, StoredAccessToken] = Field(default_factory=dict)
    refresh_tokens: dict[str, StoredRefreshToken] = Field(default_factory=dict)

    def prune_expired(self, now: float | None = None) -> bool:
        """Remove expired pending requests, codes, and tokens. Returns True if mutated."""
        ts = time.time() if now is None else now
        mutated = False
        for mapping in (self.pending, self.auth_codes, self.access_tokens, self.refresh_tokens):
            expired_keys = [k for k, v in mapping.items() if v.expires_at <= ts]
            for key in expired_keys:
                del mapping[key]
                mutated = True
        return mutated
