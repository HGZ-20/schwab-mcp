"""Configuration management for schwab-mcp (XDG-compliant, env & CLI overrideable)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LOCAL_CALLBACK_URL = "https://127.0.0.1:8182"


def default_data_dir() -> Path:
    """Return the user-level data/config directory shared across local MCP agents."""
    explicit = os.getenv("SCHWAB_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / "schwab-mcp"
    xdg = os.getenv("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "schwab-mcp"
    return Path.home() / ".config" / "schwab-mcp"


def resolve_env_file() -> Path | None:
    """Locate an optional .env file without coupling to the package installation directory."""
    explicit = os.getenv("SCHWAB_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser()

    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        return cwd_env

    user_env = default_data_dir() / ".env"
    if user_env.is_file():
        return user_env

    return None


def _default_token_path() -> Path:
    return default_data_dir() / "token.json"


def _default_audit_log_path() -> Path:
    return default_data_dir() / "trade_audit.jsonl"


def _default_oauth_state_path() -> Path:
    return default_data_dir() / "oauth_state.json"


class Settings(BaseSettings):
    """Application settings loaded from environment variables, .env, or CLI flags."""

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def __init__(self, **values: Any) -> None:
        if "_env_file" not in values:
            env_file = resolve_env_file()
            if env_file is not None:
                values["_env_file"] = env_file
        super().__init__(**values)

    # ------------------------------------------------------------------
    # Charles Schwab Developer App credentials
    # ------------------------------------------------------------------
    schwab_app_key: str = Field(
        default="",
        description="Charles Schwab Developer App Key (Client ID)",
    )
    schwab_app_secret: str = Field(
        default="",
        description="Charles Schwab Developer App Secret",
    )
    schwab_callback_url: str = Field(
        default="",
        description=(
            "OAuth Callback URL registered in Schwab Developer Portal. "
            "Auto-resolves to <MCP_PUBLIC_URL>/schwab/callback in remote OAuth mode, "
            "or https://127.0.0.1:8182 in local stdio mode."
        ),
    )
    schwab_default_account: str = Field(
        default="",
        description="Default Schwab account number, last 4 digits, or encrypted hashValue",
    )

    # ------------------------------------------------------------------
    # Persistent storage & optional local TLS certs
    # ------------------------------------------------------------------
    schwab_token_path: Path = Field(
        default_factory=_default_token_path,
        description="Path to persist Schwab OAuth tokens (0600 permissions)",
    )
    schwab_audit_log_path: Path = Field(
        default_factory=_default_audit_log_path,
        description="Path to JSONL audit log for trading operations",
    )
    mcp_oauth_state_path: Path = Field(
        default_factory=_default_oauth_state_path,
        description="Path to persist MCP OAuth 2.1 client registrations and tokens (0600)",
    )
    schwab_tls_cert_path: Path | None = Field(
        default=None,
        description="Optional PEM TLS certificate for local HTTPS callback listener (e.g. mkcert)",
    )
    schwab_tls_key_path: Path | None = Field(
        default=None,
        description="Optional PEM TLS private key for local HTTPS callback listener",
    )

    # ------------------------------------------------------------------
    # Risk & safety guardrails
    # ------------------------------------------------------------------
    schwab_dry_run: bool = Field(
        default=True,
        description="Global dry-run guard; forces Schwab previewOrder instead of live submission",
    )
    schwab_read_only: bool = Field(
        default=False,
        description="Global read-only guard; blocks live order placement, replacement, and cancellation",
    )
    schwab_max_order_usd: float = Field(
        default=5000.0,
        gt=0,
        description="Maximum allowed notional USD value for a single order",
    )
    schwab_allowed_symbols: str = Field(
        default="",
        description="Optional comma-separated whitelist of underlying ticker symbols",
    )
    schwab_blocked_symbols: str = Field(
        default="",
        description="Optional comma-separated blacklist of underlying ticker symbols",
    )

    # ------------------------------------------------------------------
    # Schwab HTTP client settings
    # ------------------------------------------------------------------
    schwab_api_timeout: float = Field(default=20.0, gt=0)

    # ------------------------------------------------------------------
    # MCP server transport & OAuth 2.1 remote hosting
    # ------------------------------------------------------------------
    mcp_transport: Literal["stdio", "streamable-http"] = Field(
        default="stdio",
        description="MCP transport mode: stdio | streamable-http",
    )
    mcp_host: str = Field(default="127.0.0.1")
    mcp_port: int = Field(default=8765, ge=1, le=65535)
    mcp_streamable_http_path: str = Field(default="/mcp")
    mcp_stateless_http: bool = Field(
        default=False,
        description="Force purely stateless HTTP transport for horizontally scaled deployments",
    )

    mcp_public_url: str = Field(
        default="",
        description=(
            "Public HTTPS base URL for Mode 1 remote OAuth 2.1 hosting "
            "(e.g. https://schwab-mcp.example.com)"
        ),
    )
    mcp_auth_mode: Literal["auto", "oauth", "bearer", "none"] = Field(
        default="auto",
        description="HTTP authentication mode: auto | oauth | bearer | none",
    )
    mcp_http_auth_token: str = Field(
        default="",
        description="Optional static Bearer token for non-OAuth HTTP deployments",
    )
    mcp_oauth_access_token_ttl: int = Field(default=3600, ge=60)
    mcp_oauth_code_ttl: int = Field(default=300, ge=30)
    mcp_oauth_pending_ttl: int = Field(default=600, ge=60)

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------
    @property
    def normalized_public_url(self) -> str:
        return self.mcp_public_url.strip().rstrip("/")

    @property
    def normalized_http_path(self) -> str:
        path = self.mcp_streamable_http_path.strip() or "/mcp"
        return path if path.startswith("/") else f"/{path}"

    @property
    def resource_server_url(self) -> str:
        if not self.normalized_public_url:
            return ""
        return f"{self.normalized_public_url}{self.normalized_http_path}"

    @property
    def effective_http_auth_mode(self) -> Literal["oauth", "bearer", "none"]:
        if self.mcp_auth_mode != "auto":
            return self.mcp_auth_mode
        if self.normalized_public_url:
            return "oauth"
        if self.mcp_http_auth_token.strip():
            return "bearer"
        return "none"

    @property
    def effective_callback_url(self) -> str:
        explicit = self.schwab_callback_url.strip()
        if explicit:
            return explicit
        if self.normalized_public_url and self.effective_http_auth_mode == "oauth":
            return f"{self.normalized_public_url}/schwab/callback"
        return DEFAULT_LOCAL_CALLBACK_URL

    @property
    def is_loopback_callback(self) -> bool:
        parsed = urlparse(self.effective_callback_url)
        return (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}

    @property
    def certs_dir(self) -> Path:
        return self.schwab_token_path.expanduser().parent / "certs"

    @property
    def allowed_symbol_set(self) -> set[str]:
        return {s.strip().upper() for s in self.schwab_allowed_symbols.split(",") if s.strip()}

    @property
    def blocked_symbol_set(self) -> set[str]:
        return {s.strip().upper() for s in self.schwab_blocked_symbols.split(",") if s.strip()}

    def has_credentials(self) -> bool:
        return bool(self.schwab_app_key.strip() and self.schwab_app_secret.strip())

    def risk_summary(self) -> dict[str, object]:
        return {
            "dry_run": self.schwab_dry_run,
            "read_only": self.schwab_read_only,
            "max_order_usd": self.schwab_max_order_usd,
            "allowed_symbols": sorted(self.allowed_symbol_set),
            "blocked_symbols": sorted(self.blocked_symbol_set),
            "audit_log_path": str(self.schwab_audit_log_path.expanduser()),
        }
