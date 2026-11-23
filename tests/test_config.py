"""Unit tests for configuration resolution, XDG defaults, and auth mode auto-detection."""

from __future__ import annotations

from pathlib import Path

from schwab_mcp.config import DEFAULT_LOCAL_CALLBACK_URL, Settings, default_data_dir


def test_default_data_dir_respects_env(monkeypatch, tmp_path: Path) -> None:
    custom_xdg = tmp_path / "xdg"
    monkeypatch.delenv("SCHWAB_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(custom_xdg))
    assert default_data_dir() == custom_xdg / "schwab-mcp"

    explicit = tmp_path / "explicit_dir"
    monkeypatch.setenv("SCHWAB_DATA_DIR", str(explicit))
    assert default_data_dir() == explicit


def test_auto_auth_mode_and_callback_resolution(tmp_path: Path) -> None:
    # Mode 2 local default
    local = Settings(
        schwab_app_key="k",
        schwab_app_secret="s",
        schwab_callback_url="",
        mcp_public_url="",
        mcp_http_auth_token="",
        schwab_token_path=tmp_path / "token.json",
    )
    assert local.effective_http_auth_mode == "none"
    assert local.effective_callback_url == DEFAULT_LOCAL_CALLBACK_URL
    assert local.is_loopback_callback is True

    # Mode 1 remote OAuth auto-detected from MCP_PUBLIC_URL
    remote = Settings(
        schwab_app_key="k",
        schwab_app_secret="s",
        schwab_callback_url="",
        mcp_public_url="https://schwab.example.com/",
        mcp_http_auth_token="",
        schwab_token_path=tmp_path / "token.json",
    )
    assert remote.normalized_public_url == "https://schwab.example.com"
    assert remote.resource_server_url == "https://schwab.example.com/mcp"
    assert remote.effective_http_auth_mode == "oauth"
    assert remote.effective_callback_url == "https://schwab.example.com/schwab/callback"
    assert remote.is_loopback_callback is False

    # Explicit callback URL always wins
    custom_cb = remote.model_copy(
        update={"schwab_callback_url": "https://auth.example.com/custom-cb"}
    )
    assert custom_cb.effective_callback_url == "https://auth.example.com/custom-cb"

    # Static Bearer mode when MCP_HTTP_AUTH_TOKEN set without MCP_PUBLIC_URL
    bearer = Settings(
        schwab_app_key="k",
        schwab_app_secret="s",
        mcp_public_url="",
        mcp_http_auth_token="secret-123",
        schwab_token_path=tmp_path / "token.json",
    )
    assert bearer.effective_http_auth_mode == "bearer"
