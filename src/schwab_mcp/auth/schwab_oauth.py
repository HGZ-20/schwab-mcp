"""Charles Schwab OAuth 2.0 token lifecycle management (local & multi-subject aware)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token

from ..config import Settings
from ..exceptions import SchwabAuthError
from .models import (
    DEFAULT_ACCESS_LIFETIME_SEC,
    REFRESH_TOKEN_LIFETIME_SEC,
    TokenData,
)
from .storage import DEFAULT_SUBJECT, SchwabTokenStore

SCHWAB_AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
SCHWAB_TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_subject_from_token(token: TokenData) -> str:
    """Derive a stable, privacy-preserving subject ID from Schwab's id_token if present."""
    if token.id_token:
        parts = token.id_token.split(".")
        if len(parts) >= 2:
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            try:
                claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
                raw_sub = claims.get("sub")
                if isinstance(raw_sub, str) and raw_sub.strip():
                    digest = hashlib.sha256(raw_sub.strip().encode("utf-8")).hexdigest()[:24]
                    return f"schwab_{digest}"
            except Exception:
                pass
    return f"schwab_{secrets.token_hex(12)}"


class SchwabAuthManager:
    """Manages Schwab OAuth 2.0 authorization, per-subject persistence, and proactive refresh."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        store: SchwabTokenStore | None = None,
    ) -> None:
        self.settings = settings
        self._external_http_client = http_client
        self.store = store or SchwabTokenStore(settings.schwab_token_path)
        self._locks: dict[str, asyncio.Lock] = {}
        self._cache: dict[str, TokenData] = {}
        self._pending_local_states: dict[str, float] = {}

    @property
    def token_path(self) -> Path:
        return self.store.path

    def current_subject(self, subject: str | None = None) -> str:
        """Resolve the active Schwab token subject from explicit arg or MCP OAuth context."""
        if subject is not None:
            return subject
        mcp_token = get_access_token()
        if mcp_token is not None and mcp_token.subject:
            return mcp_token.subject
        return DEFAULT_SUBJECT

    def _get_lock(self, subject: str) -> asyncio.Lock:
        lock = self._locks.get(subject)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[subject] = lock
        return lock

    def _require_credentials(self) -> tuple[str, str]:
        if not self.settings.has_credentials():
            raise SchwabAuthError(
                "Missing SCHWAB_APP_KEY or SCHWAB_APP_SECRET. "
                "Pass them via environment variables, .env, or --app-key / --app-secret."
            )
        return self.settings.schwab_app_key.strip(), self.settings.schwab_app_secret.strip()

    def _basic_auth_header(self) -> str:
        app_key, app_secret = self._require_credentials()
        raw = f"{app_key}:{app_secret}".encode()
        encoded = base64.b64encode(raw).decode("ascii")
        return f"Basic {encoded}"

    def build_authorization_url(
        self,
        *,
        state: str | None = None,
        redirect_uri: str | None = None,
    ) -> str:
        """Build the Charles Schwab OAuth 2.0 authorization URL."""
        app_key, _ = self._require_credentials()
        params: dict[str, str] = {
            "client_id": app_key,
            "redirect_uri": redirect_uri or self.settings.effective_callback_url,
            "response_type": "code",
        }
        if state:
            params["state"] = state
        return f"{SCHWAB_AUTHORIZE_URL}?{urlencode(params)}"

    def issue_local_state(self, ttl_sec: int = 600) -> str:
        """Create and track a short-lived CSRF state token for local interactive auth."""
        now = time.time()
        self._pending_local_states = {
            k: exp for k, exp in self._pending_local_states.items() if exp > now
        }
        state = secrets.token_urlsafe(24)
        self._pending_local_states[state] = now + ttl_sec
        return state

    def consume_local_state(self, state: str | None) -> bool:
        """Validate and consume a local CSRF state token if present."""
        if not state:
            return False
        now = time.time()
        exp = self._pending_local_states.pop(state, None)
        return exp is not None and exp > now

    @staticmethod
    def extract_callback_params(callback_url_or_code: str) -> tuple[str, str | None]:
        """Extract (code, optional_state) from a full callback URL, query string, or raw code."""
        text = callback_url_or_code.strip()
        if not text:
            raise ValueError("Empty callback URL or authorization code.")

        if "://" in text or text.startswith("?") or "code=" in text:
            parsed = urlparse(text if "://" in text else f"https://localhost/?{text.lstrip('?')}")
            params = parse_qs(parsed.query)
            errors = params.get("error")
            if errors and errors[0].strip():
                desc = (params.get("error_description") or [""])[0].strip()
                detail = f": {desc}" if desc else ""
                raise SchwabAuthError(f"Schwab OAuth returned error '{errors[0].strip()}'{detail}")
            codes = params.get("code")
            if not codes or not codes[0].strip():
                raise ValueError(
                    "Could not find 'code' query parameter in the pasted callback URL."
                )
            states = params.get("state")
            state = states[0].strip() if states and states[0].strip() else None
            return codes[0].strip(), state

        return unquote(text), None

    @classmethod
    def extract_auth_code(cls, callback_url_or_code: str) -> str:
        """Extract and URL-decode the Schwab authorization code from a callback URL or raw code."""
        code, _ = cls.extract_callback_params(callback_url_or_code)
        return code

    def load_token(self, subject: str | None = None) -> TokenData | None:
        """Load token data from disk for the active or specified subject."""
        subj = self.current_subject(subject)
        token = self.store.load(subj)
        if token is not None:
            self._cache[subj] = token
        else:
            self._cache.pop(subj, None)
        return token

    def save_token(self, token: TokenData, subject: str | None = None) -> None:
        """Atomically persist token data to disk with 0600 permissions."""
        subj = self.current_subject(subject)
        self.store.save(token, subj)
        self._cache[subj] = token

    def delete_token(self, subject: str | None = None) -> bool:
        """Delete stored token data for the active or specified subject."""
        subj = self.current_subject(subject)
        self._cache.pop(subj, None)
        return self.store.delete(subj)

    async def _post_token_request(self, form_data: dict[str, str]) -> dict[str, Any]:
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if self._external_http_client is not None:
            resp = await self._external_http_client.post(
                SCHWAB_TOKEN_URL, headers=headers, data=form_data
            )
        else:
            async with httpx.AsyncClient(timeout=self.settings.schwab_api_timeout) as client:
                resp = await client.post(SCHWAB_TOKEN_URL, headers=headers, data=form_data)

        if resp.status_code != 200:
            raise SchwabAuthError(
                f"Schwab OAuth token request failed ({resp.status_code}): {resp.text}"
            )
        data: dict[str, Any] = resp.json()
        return data

    async def exchange_code(
        self,
        callback_url_or_code: str,
        *,
        subject: str | None = None,
        redirect_uri: str | None = None,
        persist: bool = True,
        expected_state: str | None = None,
    ) -> TokenData:
        """Exchange an OAuth authorization code for Schwab access and refresh tokens."""
        code, returned_state = self.extract_callback_params(callback_url_or_code)
        if (
            expected_state is not None
            and returned_state is not None
            and returned_state != expected_state
        ):
            raise SchwabAuthError("OAuth state mismatch. Please restart authentication.")

        now = time.time()
        payload = await self._post_token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri or self.settings.effective_callback_url,
            }
        )
        expires_in = int(payload.get("expires_in", DEFAULT_ACCESS_LIFETIME_SEC))
        token = TokenData(
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
            token_type=payload.get("token_type", "Bearer"),
            scope=payload.get("scope", "api"),
            expires_in=expires_in,
            id_token=payload.get("id_token"),
            obtained_at=now,
            access_expires_at=now + expires_in,
            refresh_obtained_at=now,
            refresh_expires_at=now + REFRESH_TOKEN_LIFETIME_SEC,
        )
        if persist:
            self.save_token(token, subject=subject)
        return token

    async def refresh_access_token(self, subject: str | None = None) -> TokenData:
        """Refresh the 30-minute access token using the stored 7-day refresh token."""
        subj = self.current_subject(subject)
        async with self._get_lock(subj):
            token = self.load_token(subj) or self._cache.get(subj)
            if token is None:
                raise SchwabAuthError(
                    "Not authenticated with Charles Schwab. "
                    "Call the `start_auth` tool (or run `schwab-mcp auth login`) to authenticate."
                )
            return await self._refresh_locked(token, subj)

    async def _refresh_locked(self, token: TokenData, subj: str) -> TokenData:
        now = time.time()
        if token.refresh_seconds_remaining(now) <= 0:
            raise SchwabAuthError(
                "Charles Schwab 7-day refresh token has expired. "
                "Re-authenticate using the `start_auth` tool (or `schwab-mcp auth login`)."
            )

        payload = await self._post_token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
            }
        )
        expires_in = int(payload.get("expires_in", DEFAULT_ACCESS_LIFETIME_SEC))
        # Preserve original 7-day refresh token expiry window accurately.
        updated = TokenData(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token") or token.refresh_token,
            token_type=payload.get("token_type", token.token_type),
            scope=payload.get("scope", token.scope),
            expires_in=expires_in,
            id_token=payload.get("id_token", token.id_token),
            obtained_at=now,
            access_expires_at=now + expires_in,
            refresh_obtained_at=token.refresh_obtained_at,
            refresh_expires_at=token.refresh_expires_at,
        )
        self.save_token(updated, subject=subj)
        return updated

    async def get_valid_access_token(
        self,
        *,
        force_refresh: bool = False,
        subject: str | None = None,
    ) -> str:
        """Return a valid Schwab access token, proactively refreshing before expiration."""
        subj = self.current_subject(subject)
        if not force_refresh:
            token = self._cache.get(subj) or self.load_token(subj)
            if token is not None and not token.needs_access_refresh():
                return token.access_token

        async with self._get_lock(subj):
            token = self.load_token(subj) or self._cache.get(subj)
            if token is None:
                raise SchwabAuthError(
                    "Not authenticated with Charles Schwab. "
                    "Call the `start_auth` tool (or run `schwab-mcp auth login`) to authenticate."
                )
            if not force_refresh and not token.needs_access_refresh():
                return token.access_token

            updated = await self._refresh_locked(token, subj)
            return updated.access_token

    def has_valid_refresh_token(self, subject: str | None = None) -> bool:
        token = self.load_token(subject)
        return token is not None and token.refresh_seconds_remaining() > 0

    def get_status(self, subject: str | None = None) -> dict[str, Any]:
        """Return safe, non-secret authentication health status for the active subject."""
        subj = self.current_subject(subject)
        now = time.time()
        status: dict[str, Any] = {
            "credentials_configured": self.settings.has_credentials(),
            "callback_url": self.settings.effective_callback_url,
            "token_path": str(self.token_path),
            "subject": subj,
            "token_exists": self.store.exists(),
        }

        token = self.load_token(subj) if self.store.exists() else None
        if token is None:
            status["authenticated"] = False
            status["action_required"] = (
                "Call `start_auth` (in local mode) or reconnect via MCP OAuth 2.1, "
                "or run `schwab-mcp auth login`."
            )
            return status

        access_rem = token.access_seconds_remaining(now)
        refresh_rem = token.refresh_seconds_remaining(now)

        warnings: list[str] = []
        if refresh_rem <= 0:
            warnings.append(
                "Schwab 7-day refresh token has expired. Call `start_auth` or re-authorize."
            )
        elif refresh_rem < 24 * 3600:
            warnings.append(
                f"Schwab refresh token expires in {refresh_rem / 3600:.1f} hours. "
                "Re-authenticate soon to avoid interruption."
            )

        status.update(
            {
                "authenticated": refresh_rem > 0,
                "access_token_valid": access_rem > 0,
                "access_expires_at_utc": _iso_utc(token.access_expires_at),
                "access_expires_in_minutes": round(max(access_rem, 0.0) / 60, 1),
                "refresh_token_valid": refresh_rem > 0,
                "refresh_expires_at_utc": _iso_utc(token.refresh_expires_at),
                "refresh_expires_in_hours": round(max(refresh_rem, 0.0) / 3600, 1),
                "warnings": warnings,
            }
        )
        return status
