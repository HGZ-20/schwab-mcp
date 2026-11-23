"""MCP OAuth 2.1 Authorization Server proxying Charles Schwab OAuth 2.0 (Mode 1 Remote Hosting).

Enables a Cloudflare-like one-click OAuth experience in Cursor, Codex, Claude Desktop,
Claude Code, and any MCP 2026-07-28 compatible client:
  Client -> 401 + Protected Resource Metadata -> DCR (/register) -> /authorize (PKCE)
         -> Charles Schwab Login -> /schwab/callback -> Client redirect_uri
         -> /token -> Authenticated /mcp calls (with automatic refresh & revocation).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl, ValidationError

from ..config import Settings
from .models import (
    OAuthProviderState,
    PendingUpstreamAuth,
    StoredAccessToken,
    StoredAuthCode,
    StoredRefreshToken,
)
from .schwab_oauth import SchwabAuthManager, derive_subject_from_token
from .storage import OAuthStateStore

DEFAULT_MCP_SCOPE = "schwab:api"


def _normalize_url_str(url: str | None) -> str:
    if not url:
        return ""
    try:
        return str(AnyHttpUrl(url)).rstrip("/")
    except ValidationError:
        return url.strip().rstrip("/")


class SchwabOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """MCP OAuth 2.1 Authorization Server backed by Charles Schwab OAuth 2.0."""

    def __init__(
        self,
        settings: Settings,
        schwab_auth: SchwabAuthManager,
        *,
        state_store: OAuthStateStore | None = None,
    ) -> None:
        self.settings = settings
        self.schwab_auth = schwab_auth
        self.store = state_store or OAuthStateStore(settings.mcp_oauth_state_path)
        self._lock = asyncio.Lock()
        self._state: OAuthProviderState = self.store.load()

    def _save(self) -> None:
        self.store.save(self._state)

    def _resolve_resource(self, requested_resource: str | None) -> str:
        expected = _normalize_url_str(self.settings.resource_server_url)
        if not requested_resource:
            return expected
        req_norm = _normalize_url_str(requested_resource)
        if expected and req_norm != expected:
            raise AuthorizeError(
                error="invalid_target",
                error_description=(
                    f"Requested resource {requested_resource!r} does not match "
                    f"this MCP server ({expected})."
                ),
            )
        return expected or req_norm

    # ------------------------------------------------------------------
    # Dynamic Client Registration (RFC 7591)
    # ------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with self._lock:
            raw = self._state.clients.get(client_id)
            if raw is None:
                return None
            return OAuthClientInformationFull.model_validate(raw)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with self._lock:
            if not client_info.client_id:
                raise ValueError("Registered OAuth client must include a client_id.")
            self._state.clients[client_info.client_id] = client_info.model_dump(mode="json")
            self._save()

    # ------------------------------------------------------------------
    # Authorization Endpoint (/authorize -> Charles Schwab OAuth)
    # ------------------------------------------------------------------

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        if not self.settings.has_credentials():
            raise AuthorizeError(
                error="server_error",
                error_description="Server is missing SCHWAB_APP_KEY or SCHWAB_APP_SECRET.",
            )

        resource = self._resolve_resource(params.resource)
        scopes = list(params.scopes) if params.scopes else [DEFAULT_MCP_SCOPE]
        upstream_state = secrets.token_urlsafe(32)
        expires_at = time.time() + self.settings.mcp_oauth_pending_ttl

        pending = PendingUpstreamAuth(
            upstream_state=upstream_state,
            client_id=client.client_id or "",
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            client_state=params.state,
            scopes=scopes,
            resource=resource or None,
            expires_at=expires_at,
        )

        async with self._lock:
            self._state.prune_expired()
            self._state.pending[upstream_state] = pending
            self._save()

        return self.schwab_auth.build_authorization_url(
            state=upstream_state,
            redirect_uri=self.settings.effective_callback_url,
        )

    # ------------------------------------------------------------------
    # Upstream Callback Handler (/schwab/callback -> MCP Client redirect)
    # ------------------------------------------------------------------

    async def handle_schwab_callback(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> str:
        """Complete upstream Schwab OAuth and return the redirect URL back to the MCP client."""
        if not state:
            raise AuthorizeError(
                error="invalid_request",
                error_description="Missing OAuth state parameter in Schwab callback.",
            )

        async with self._lock:
            self._state.prune_expired()
            pending = self._state.pending.pop(state, None)
            if pending is not None:
                self._save()

        if pending is None or pending.expires_at <= time.time():
            raise AuthorizeError(
                error="invalid_request",
                error_description="OAuth authorization session expired or unknown. Please reconnect.",
            )

        if error:
            return construct_redirect_uri(
                pending.redirect_uri,
                error="access_denied" if error == "access_denied" else "server_error",
                error_description=error_description or f"Schwab authorization failed: {error}",
                state=pending.client_state,
            )

        if not code:
            return construct_redirect_uri(
                pending.redirect_uri,
                error="server_error",
                error_description="Schwab callback did not include an authorization code.",
                state=pending.client_state,
            )

        try:
            schwab_token = await self.schwab_auth.exchange_code(
                code,
                redirect_uri=self.settings.effective_callback_url,
                persist=False,
            )
        except Exception as exc:
            return construct_redirect_uri(
                pending.redirect_uri,
                error="server_error",
                error_description=f"Failed to exchange Schwab authorization code: {exc}",
                state=pending.client_state,
            )

        subject = derive_subject_from_token(schwab_token)
        self.schwab_auth.save_token(schwab_token, subject=subject)

        # RFC 6749 §10.10: >= 160 bits of entropy (token_urlsafe(32) = 256 bits)
        mcp_code = secrets.token_urlsafe(32)
        stored_code = StoredAuthCode(
            code=mcp_code,
            client_id=pending.client_id,
            scopes=pending.scopes,
            expires_at=time.time() + self.settings.mcp_oauth_code_ttl,
            code_challenge=pending.code_challenge,
            redirect_uri=pending.redirect_uri,
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            resource=pending.resource,
            subject=subject,
        )

        async with self._lock:
            self._state.auth_codes[mcp_code] = stored_code
            self._save()

        return construct_redirect_uri(
            pending.redirect_uri,
            code=mcp_code,
            state=pending.client_state,
        )

    # ------------------------------------------------------------------
    # Authorization Code Exchange (/token grant_type=authorization_code)
    # ------------------------------------------------------------------

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        async with self._lock:
            self._state.prune_expired()
            stored = self._state.auth_codes.get(authorization_code)
            if stored is None or stored.client_id != client.client_id:
                return None
            return AuthorizationCode(
                code=stored.code,
                scopes=stored.scopes,
                expires_at=stored.expires_at,
                client_id=stored.client_id,
                code_challenge=stored.code_challenge,
                redirect_uri=AnyUrl(stored.redirect_uri),
                redirect_uri_provided_explicitly=stored.redirect_uri_provided_explicitly,
                resource=stored.resource,
                subject=stored.subject,
            )

    def _mint_token_pair(
        self,
        *,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        subject: str,
    ) -> OAuthToken:
        now = int(time.time())
        schwab_token = self.schwab_auth.load_token(subject)
        if schwab_token is None or schwab_token.refresh_seconds_remaining(now) <= 0:
            raise TokenError(
                error="invalid_grant",
                error_description=(
                    "Charles Schwab 7-day refresh token is missing or expired. "
                    "Please re-authorize your Schwab account."
                ),
            )

        access_ttl = min(
            self.settings.mcp_oauth_access_token_ttl,
            max(60, int(schwab_token.refresh_seconds_remaining(now))),
        )
        access_exp = now + access_ttl
        refresh_exp = int(schwab_token.refresh_expires_at)

        access_str = secrets.token_urlsafe(32)
        refresh_str = secrets.token_urlsafe(32)

        self._state.access_tokens[access_str] = StoredAccessToken(
            token=access_str,
            client_id=client_id,
            scopes=scopes,
            expires_at=access_exp,
            resource=resource,
            subject=subject,
            refresh_token_id=refresh_str,
        )
        self._state.refresh_tokens[refresh_str] = StoredRefreshToken(
            token=refresh_str,
            client_id=client_id,
            scopes=scopes,
            expires_at=refresh_exp,
            resource=resource,
            subject=subject,
            access_token_id=access_str,
        )
        return OAuthToken(
            access_token=access_str,
            token_type="Bearer",
            expires_in=access_ttl,
            scope=" ".join(scopes),
            refresh_token=refresh_str,
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        async with self._lock:
            stored = self._state.auth_codes.pop(authorization_code.code, None)
            if stored is None or stored.client_id != client.client_id:
                raise TokenError(
                    error="invalid_grant",
                    error_description="Authorization code is invalid or already used.",
                )
            token = self._mint_token_pair(
                client_id=stored.client_id,
                scopes=stored.scopes,
                resource=stored.resource,
                subject=stored.subject,
            )
            self._save()
            return token

    # ------------------------------------------------------------------
    # Refresh Token Rotation (/token grant_type=refresh_token)
    # ------------------------------------------------------------------

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        async with self._lock:
            self._state.prune_expired()
            stored = self._state.refresh_tokens.get(refresh_token)
            if stored is None or stored.client_id != client.client_id:
                return None
            return RefreshToken(
                token=stored.token,
                client_id=stored.client_id,
                scopes=stored.scopes,
                expires_at=stored.expires_at,
                resource=stored.resource,
                subject=stored.subject,
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        async with self._lock:
            stored = self._state.refresh_tokens.pop(refresh_token.token, None)
            if stored is None or stored.client_id != client.client_id:
                raise TokenError(
                    error="invalid_grant",
                    error_description="Refresh token is invalid or revoked.",
                )
            if stored.access_token_id:
                self._state.access_tokens.pop(stored.access_token_id, None)

            granted_scopes = list(scopes) if scopes else list(stored.scopes)
            token = self._mint_token_pair(
                client_id=stored.client_id,
                scopes=granted_scopes,
                resource=stored.resource,
                subject=stored.subject,
            )
            self._save()
            return token

    # ------------------------------------------------------------------
    # Access Token Verification & Revocation (/revoke)
    # ------------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with self._lock:
            now = int(time.time())
            stored = self._state.access_tokens.get(token)
            if stored is None:
                return None
            if stored.expires_at <= now:
                self._state.access_tokens.pop(token, None)
                self._save()
                return None
            if not self.schwab_auth.has_valid_refresh_token(stored.subject):
                return None
            return AccessToken(
                token=stored.token,
                client_id=stored.client_id,
                scopes=stored.scopes,
                expires_at=stored.expires_at,
                resource=stored.resource,
                subject=stored.subject,
                claims={"iss": self.settings.normalized_public_url}
                if self.settings.normalized_public_url
                else None,
            )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with self._lock:
            subject: str | None = None
            if isinstance(token, AccessToken):
                stored_at = self._state.access_tokens.pop(token.token, None)
                if stored_at:
                    subject = stored_at.subject
                    if stored_at.refresh_token_id:
                        self._state.refresh_tokens.pop(stored_at.refresh_token_id, None)
            else:
                stored_rt = self._state.refresh_tokens.pop(token.token, None)
                if stored_rt:
                    subject = stored_rt.subject
                    if stored_rt.access_token_id:
                        self._state.access_tokens.pop(stored_rt.access_token_id, None)

            if subject is not None:
                still_referenced = any(
                    t.subject == subject for t in self._state.access_tokens.values()
                ) or any(t.subject == subject for t in self._state.refresh_tokens.values())
                if not still_referenced:
                    self.schwab_auth.delete_token(subject)

            self._save()

    def stats(self) -> dict[str, Any]:
        """Return safe, non-secret diagnostics for the OAuth 2.1 provider."""
        self._state.prune_expired()
        return {
            "registered_clients": len(self._state.clients),
            "active_access_tokens": len(self._state.access_tokens),
            "active_refresh_tokens": len(self._state.refresh_tokens),
            "pending_authorizations": len(self._state.pending),
        }
