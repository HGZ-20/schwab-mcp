"""Unit tests for Schwab OAuth 2.0 token lifecycle, storage, and multi-subject isolation."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import httpx
import pytest
import respx

from schwab_mcp.auth.models import TokenData
from schwab_mcp.auth.schwab_oauth import (
    SCHWAB_TOKEN_URL,
    SchwabAuthManager,
    derive_subject_from_token,
)
from schwab_mcp.auth.storage import DEFAULT_SUBJECT, SchwabTokenStore
from schwab_mcp.config import Settings
from schwab_mcp.exceptions import SchwabAuthError


def test_build_authorization_url_and_extract_params(base_settings: Settings) -> None:
    auth = SchwabAuthManager(base_settings)
    url = auth.build_authorization_url(state="st_123")
    assert "client_id=test_app_key" in url
    assert "redirect_uri=https%3A%2F%2F127.0.0.1%3A8182" in url
    assert "response_type=code" in url
    assert "state=st_123" in url

    callback = "https://127.0.0.1:8182/?code=C0.b2F1dGgy.abc123%40&state=st_123&session=xyz"
    code, state = auth.extract_callback_params(callback)
    assert code == "C0.b2F1dGgy.abc123@"
    assert state == "st_123"
    assert auth.extract_auth_code("C0.raw%40") == "C0.raw@"

    with pytest.raises(ValueError):
        auth.extract_auth_code("https://127.0.0.1:8182/?session=no_code")

    with pytest.raises(SchwabAuthError, match="access_denied"):
        auth.extract_callback_params("https://127.0.0.1:8182/?error=access_denied")


@respx.mock
async def test_exchange_and_proactive_refresh_preserves_7day_expiry(
    base_settings: Settings,
) -> None:
    respx.post(SCHWAB_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "access_token": "acc_1",
                    "refresh_token": "ref_1",
                    "token_type": "Bearer",
                    "expires_in": 1800,
                    "scope": "api",
                },
            ),
            httpx.Response(
                200,
                json={
                    "access_token": "acc_2",
                    "refresh_token": "ref_2",
                    "token_type": "Bearer",
                    "expires_in": 1800,
                    "scope": "api",
                },
            ),
        ]
    )

    async with httpx.AsyncClient() as http:
        auth = SchwabAuthManager(base_settings, http_client=http)
        tok1 = await auth.exchange_code("https://127.0.0.1:8182/?code=abc%40")
        assert tok1.access_token == "acc_1"
        assert (os.stat(base_settings.schwab_token_path).st_mode & 0o777) == 0o600

        tok1.access_expires_at = time.time() + 60
        auth.save_token(tok1)

        token_str = await auth.get_valid_access_token()
        assert token_str == "acc_2"

        reloaded = auth.load_token()
        assert reloaded is not None
        assert reloaded.refresh_token == "ref_2"
        assert reloaded.refresh_expires_at == tok1.refresh_expires_at


async def test_expired_refresh_token_raises_actionable_error(base_settings: Settings) -> None:
    auth = SchwabAuthManager(base_settings)
    expired = TokenData(
        access_token="old_acc",
        refresh_token="old_ref",
        obtained_at=time.time() - 900000,
        access_expires_at=time.time() - 800000,
        refresh_obtained_at=time.time() - 900000,
        refresh_expires_at=time.time() - 10,
    )
    auth.save_token(expired)

    with pytest.raises(SchwabAuthError, match="7-day refresh token has expired"):
        await auth.get_valid_access_token()

    status = auth.get_status()
    assert status["authenticated"] is False
    assert status["warnings"]


def test_token_store_canonical_schema_and_multi_subject_isolation(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    now = time.time()
    tok_default = TokenData(
        access_token="acc_default",
        refresh_token="ref_default",
        obtained_at=now,
        access_expires_at=now + 1800,
        refresh_obtained_at=now,
        refresh_expires_at=now + 500000,
    )

    store = SchwabTokenStore(path)
    store.save(tok_default, subject=DEFAULT_SUBJECT)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert set(raw["subjects"].keys()) == {DEFAULT_SUBJECT}

    loaded = store.load(DEFAULT_SUBJECT)
    assert loaded is not None
    assert loaded.access_token == "acc_default"

    tok_user_b = tok_default.model_copy(update={"access_token": "acc_user_b"})
    store.save(tok_user_b, subject="schwab_user_b")

    assert store.load(DEFAULT_SUBJECT).access_token == "acc_default"  # type: ignore[union-attr]
    assert store.load("schwab_user_b").access_token == "acc_user_b"  # type: ignore[union-attr]
    assert store.list_subjects() == ["default", "schwab_user_b"]

    assert store.delete("schwab_user_b") is True
    assert store.load("schwab_user_b") is None
    assert store.load(DEFAULT_SUBJECT) is not None


def test_derive_subject_from_id_token_is_deterministic() -> None:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": "schwab-user-42"}).encode())
        .decode()
        .rstrip("=")
    )
    jwt = f"eyJhbGciOiJSUzI1NiJ9.{payload}.sig"
    now = time.time()
    tok = TokenData(
        access_token="a",
        refresh_token="r",
        id_token=jwt,
        obtained_at=now,
        access_expires_at=now + 1800,
        refresh_obtained_at=now,
        refresh_expires_at=now + 500000,
    )
    sub1 = derive_subject_from_token(tok)
    sub2 = derive_subject_from_token(tok)
    assert sub1 == sub2
    assert sub1.startswith("schwab_")
    assert "42" not in sub1
