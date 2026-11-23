"""Tests for unified schwab-mcp CLI parsing, overrides, and doctor command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schwab_mcp.cli import build_parser, build_settings_from_args, main


def test_cli_flag_overrides_build_settings(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--app-key",
            "cli_key",
            "--app-secret",
            "cli_secret",
            "--callback-url",
            "https://127.0.0.1:9999",
            "--token-path",
            str(tmp_path / "tok.json"),
            "--live",
            "--read-only",
            "--max-order-usd",
            "2500",
        ]
    )
    settings = build_settings_from_args(args)
    assert settings.schwab_app_key == "cli_key"
    assert settings.schwab_app_secret == "cli_secret"
    assert settings.effective_callback_url == "https://127.0.0.1:9999"
    assert settings.schwab_token_path == tmp_path / "tok.json"
    assert settings.schwab_dry_run is False
    assert settings.schwab_read_only is True
    assert settings.schwab_max_order_usd == 2500.0


def test_cli_serve_defaults_to_streamable_http(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHWAB_DATA_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("SCHWAB_CALLBACK_URL", raising=False)
    parser = build_parser()
    args = parser.parse_args(
        [
            "serve",
            "--public-url",
            "https://schwab.example.com",
            "--host",
            "0.0.0.0",
            "--port",
            "8765",
        ]
    )
    settings = build_settings_from_args(args)
    assert settings.mcp_transport == "streamable-http"
    assert settings.effective_http_auth_mode == "oauth"
    assert settings.effective_callback_url == "https://schwab.example.com/schwab/callback"


def test_cli_doctor_outputs_valid_json(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "doctor",
                "--app-key",
                "k",
                "--app-secret",
                "s",
                "--token-path",
                str(tmp_path / "token.json"),
            ]
        )
    assert exc.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["credentials_configured"] is True
    assert out["auth_status"]["authenticated"] is False
