"""Unified CLI for schwab-mcp (Mode 1 remote serve, Mode 2 local stdio, auth, doctor)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import uvicorn
from mcp.types import LATEST_PROTOCOL_VERSION

from . import __version__
from .auth.local_callback import LocalCallbackServer, can_open_browser, try_open_browser
from .auth.schwab_oauth import SchwabAuthManager
from .config import Settings
from .exceptions import SchwabAuthError
from .server.app import create_server
from .server.http import build_http_app


def _add_shared_config_args(parser: argparse.ArgumentParser) -> None:
    """Add CLI overrides so any agent can launch schwab-mcp with flags or env vars."""
    group = parser.add_argument_group("Schwab credentials & safety overrides")
    group.add_argument("--app-key", default=None, help="Schwab Developer App Key (SCHWAB_APP_KEY)")
    group.add_argument(
        "--app-secret", default=None, help="Schwab Developer App Secret (SCHWAB_APP_SECRET)"
    )
    group.add_argument(
        "--callback-url",
        default=None,
        help="OAuth Callback URL registered in Schwab Portal (SCHWAB_CALLBACK_URL)",
    )
    group.add_argument(
        "--token-path",
        default=None,
        help="Path to persist Schwab OAuth tokens (SCHWAB_TOKEN_PATH)",
    )
    group.add_argument(
        "--default-account",
        default=None,
        help="Default Schwab account number, last 4 digits, or hashValue",
    )
    group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=None,
        help="Force global dry-run/preview mode (SCHWAB_DRY_RUN=true)",
    )
    group.add_argument(
        "--live",
        dest="dry_run",
        action="store_false",
        help="Disable global dry-run guard (live orders still require confirm_live=True)",
    )
    group.add_argument(
        "--read-only",
        action="store_true",
        default=None,
        help="Block all live order placement, replacement, and cancellation",
    )
    group.add_argument(
        "--max-order-usd",
        type=float,
        default=None,
        help="Maximum single-order notional value in USD (SCHWAB_MAX_ORDER_USD)",
    )


def _add_transport_args(
    parser: argparse.ArgumentParser, *, default_transport: str | None = None
) -> None:
    group = parser.add_argument_group("MCP transport & remote hosting")
    group.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=default_transport,
        help="MCP transport mode (default: stdio, or streamable-http for `serve`)",
    )
    group.add_argument("--host", default=None, help="Bind host for HTTP mode (default: 127.0.0.1)")
    group.add_argument(
        "--port", type=int, default=None, help="Bind port for HTTP mode (default: 8765)"
    )
    group.add_argument(
        "--public-url",
        default=None,
        help="Public HTTPS base URL for Mode 1 MCP OAuth 2.1 (e.g. https://schwab-mcp.example.com)",
    )
    group.add_argument(
        "--auth-mode",
        choices=["auto", "oauth", "bearer", "none"],
        default=None,
        help="HTTP auth mode: auto | oauth | bearer | none",
    )
    group.add_argument(
        "--http-auth-token",
        default=None,
        help="Static Bearer token for HTTP mode (when not using OAuth 2.1)",
    )
    group.add_argument(
        "--stateless-http",
        action="store_true",
        default=None,
        help="Force purely stateless HTTP transport (MCP_STATELESS_HTTP=true)",
    )


def build_settings_from_args(args: argparse.Namespace) -> Settings:
    """Instantiate Settings with explicit CLI flag overrides applied on top of env/.env."""
    base = Settings()
    updates: dict[str, Any] = {}
    mapping = {
        "app_key": "schwab_app_key",
        "app_secret": "schwab_app_secret",
        "callback_url": "schwab_callback_url",
        "default_account": "schwab_default_account",
        "dry_run": "schwab_dry_run",
        "read_only": "schwab_read_only",
        "max_order_usd": "schwab_max_order_usd",
        "transport": "mcp_transport",
        "host": "mcp_host",
        "port": "mcp_port",
        "public_url": "mcp_public_url",
        "auth_mode": "mcp_auth_mode",
        "http_auth_token": "mcp_http_auth_token",
        "stateless_http": "mcp_stateless_http",
    }
    for attr, field_name in mapping.items():
        val = getattr(args, attr, None)
        if val is not None:
            updates[field_name] = val
    if getattr(args, "token_path", None):
        updates["schwab_token_path"] = Path(args.token_path).expanduser()
    return base.model_copy(update=updates) if updates else base


def _run_server(settings: Settings) -> int:
    if settings.mcp_transport == "stdio":
        mcp = create_server(settings=settings, enable_oauth=False)
        mcp.run(transport="stdio")
        return 0

    auth_mode = settings.effective_http_auth_mode
    enable_oauth = auth_mode == "oauth"

    if enable_oauth and not settings.normalized_public_url:
        print(
            "Error: MCP_AUTH_MODE=oauth requires --public-url / MCP_PUBLIC_URL "
            "(e.g. https://schwab-mcp.example.com).",
            file=sys.stderr,
        )
        return 2

    if settings.mcp_host == "0.0.0.0" and auth_mode == "none":
        print(
            "[WARNING] Binding HTTP MCP server to 0.0.0.0 without authentication! "
            "Configure --public-url (OAuth 2.1) or --http-auth-token before exposing publicly.",
            file=sys.stderr,
        )

    mcp = create_server(settings=settings, enable_oauth=enable_oauth)
    app = build_http_app(mcp, settings=settings, host=settings.mcp_host)

    if enable_oauth:
        print(
            f"[schwab-mcp] Streamable HTTP + MCP OAuth 2.1 ready\n"
            f"  MCP Endpoint:      {settings.resource_server_url}\n"
            f"  Schwab Callback:   {settings.effective_callback_url}\n"
            f"  Listening on:      http://{settings.mcp_host}:{settings.mcp_port}",
            file=sys.stderr,
        )
    else:
        print(
            f"[schwab-mcp] Streamable HTTP ready (auth={auth_mode})\n"
            f"  Listening on:      http://{settings.mcp_host}:{settings.mcp_port}"
            f"{settings.normalized_http_path}",
            file=sys.stderr,
        )

    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port)
    return 0


async def _cmd_auth_login(
    settings: Settings,
    *,
    callback_url: str | None,
    auto_callback: bool,
    open_browser: bool,
    timeout: int,
) -> int:
    auth = SchwabAuthManager(settings)
    if callback_url:
        await auth.exchange_code(callback_url)
        print("Authentication succeeded! Token saved securely (0600).")
        print(json.dumps(auth.get_status(), indent=2, ensure_ascii=False))
        return 0

    state = auth.issue_local_state()
    auth_url = auth.build_authorization_url(state=state)

    listener = LocalCallbackServer(settings)
    capture = None
    if auto_callback and settings.is_loopback_callback:

        async def _on_code(code: str) -> None:
            await auth.exchange_code(code)

        capture = await listener.start(
            expected_state=state,
            on_code=_on_code,
            timeout_sec=timeout,
        )

    opened = try_open_browser(auth_url) if open_browser else False

    print("\n=== Charles Schwab OAuth 2.0 Login ===\n")
    if opened:
        print("Opened authorization URL in your default browser:")
    else:
        print("1. Open this authorization URL in your browser and approve access:")
    print(f"\n   {auth_url}\n")

    if capture and capture.active:
        print(f"Local HTTPS callback listener active on {capture.listening_url}")
        if not capture.trusted_cert:
            print(
                "Note: Your browser may display a self-signed certificate warning on 127.0.0.1.\n"
                "      Click 'Advanced -> Proceed' to complete automatically, or press Ctrl+C "
                "and run with --no-auto-callback to paste the redirected URL manually.\n"
            )
        print(f"Waiting up to {timeout}s for Schwab callback...")
        try:
            final_status = await listener.wait_until_complete(timeout)
        except KeyboardInterrupt:
            await listener.stop()
            print("\nCancelled.", file=sys.stderr)
            return 1
        await listener.stop()

        if final_status.completed:
            print("\nAuthentication succeeded! Token saved securely (0600).")
            print(json.dumps(auth.get_status(), indent=2, ensure_ascii=False))
            return 0
        if final_status.error:
            print(f"Auto-capture did not complete: {final_status.error}", file=sys.stderr)
            return 1

    print(
        "2. After approving, copy the FULL redirected URL from your browser address bar\n"
        f"   (e.g. {settings.effective_callback_url}/?code=...&state=...) and paste it below.\n"
    )
    try:
        pasted = input("Paste callback URL (or raw code): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 1

    await auth.exchange_code(pasted, expected_state=state)
    print("\nAuthentication succeeded! Token saved securely (0600).")
    print(json.dumps(auth.get_status(), indent=2, ensure_ascii=False))
    return 0


def _cmd_doctor(settings: Settings) -> int:
    auth = SchwabAuthManager(settings)
    cert_configured = bool(
        settings.schwab_tls_cert_path
        and settings.schwab_tls_key_path
        and settings.schwab_tls_cert_path.expanduser().is_file()
        and settings.schwab_tls_key_path.expanduser().is_file()
    )
    report = {
        "version": __version__,
        "mcp_protocol_version": LATEST_PROTOCOL_VERSION,
        "credentials_configured": settings.has_credentials(),
        "effective_callback_url": settings.effective_callback_url,
        "is_loopback_callback": settings.is_loopback_callback,
        "desktop_browser_available": can_open_browser(),
        "trusted_local_tls_cert": cert_configured,
        "http_auth_mode": settings.effective_http_auth_mode,
        "public_url": settings.normalized_public_url or None,
        "resource_server_url": settings.resource_server_url or None,
        "auth_status": auth.get_status(),
        "risk_config": settings.risk_summary(),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if settings.has_credentials() else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schwab-mcp",
        description=(
            "Charles Schwab Market Data & Trading MCP Server "
            "(Mode 1 Remote OAuth 2.1 + Mode 2 Local Any-Agent stdio)"
        ),
    )
    parser.add_argument("--version", action="version", version=f"schwab-mcp {__version__}")
    _add_shared_config_args(parser)
    _add_transport_args(parser)

    sub = parser.add_subparsers(dest="command")

    # `schwab-mcp serve` (Mode 1 Remote HTTP / OAuth 2.1)
    p_serve = sub.add_parser(
        "serve",
        help="Run Streamable HTTP server (supports Mode 1 Cloudflare-style MCP OAuth 2.1)",
    )
    _add_shared_config_args(p_serve)
    _add_transport_args(p_serve, default_transport="streamable-http")

    # `schwab-mcp auth <login|status|refresh|logout>`
    p_auth = sub.add_parser("auth", help="Manage local Charles Schwab OAuth 2.0 tokens")
    _add_shared_config_args(p_auth)
    auth_sub = p_auth.add_subparsers(dest="auth_command", required=True)

    p_login = auth_sub.add_parser("login", help="Authenticate with Charles Schwab OAuth 2.0")
    p_login.add_argument(
        "--callback-url",
        dest="pasted_callback_url",
        default=None,
        help="Non-interactive full callback URL or authorization code",
    )
    p_login.add_argument(
        "--no-auto-callback",
        action="store_true",
        help="Skip local HTTPS auto-capture listener and prompt for manual URL paste",
    )
    p_login.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the system browser",
    )
    p_login.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Timeout in seconds when waiting for local HTTPS callback (default: 180)",
    )

    auth_sub.add_parser("status", help="Show current OAuth token health and remaining TTL")
    auth_sub.add_parser("refresh", help="Manually refresh the 30-minute access token")
    auth_sub.add_parser("logout", help="Delete the locally stored Schwab OAuth token")

    # `schwab-mcp doctor`
    p_doctor = sub.add_parser(
        "doctor", help="Inspect credentials, callback URL, token health, and risk guardrails"
    )
    _add_shared_config_args(p_doctor)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = build_settings_from_args(args)

    try:
        if args.command is None:
            sys.exit(_run_server(settings))
        if args.command == "serve":
            if args.transport is None:
                settings = settings.model_copy(update={"mcp_transport": "streamable-http"})
            sys.exit(_run_server(settings))
        if args.command == "doctor":
            sys.exit(_cmd_doctor(settings))
        if args.command == "auth":
            auth = SchwabAuthManager(settings)
            if args.auth_command == "login":
                code = asyncio.run(
                    _cmd_auth_login(
                        settings,
                        callback_url=args.pasted_callback_url,
                        auto_callback=not args.no_auto_callback,
                        open_browser=not args.no_browser,
                        timeout=args.timeout,
                    )
                )
                sys.exit(code)
            if args.auth_command == "status":
                print(json.dumps(auth.get_status(), indent=2, ensure_ascii=False))
                sys.exit(0)
            if args.auth_command == "refresh":
                asyncio.run(auth.refresh_access_token())
                print("Access token refreshed.")
                print(json.dumps(auth.get_status(), indent=2, ensure_ascii=False))
                sys.exit(0)
            if args.auth_command == "logout":
                removed = auth.delete_token()
                print("Local Schwab token deleted." if removed else "No local Schwab token found.")
                sys.exit(0)
    except (SchwabAuthError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
