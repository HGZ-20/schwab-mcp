"""MCPServer factory wiring tools, resources, prompts, lifespan, and OAuth 2.1 routes."""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.auth.provider import AuthorizeError
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import __version__
from ..auth.local_callback import LocalCallbackServer
from ..auth.mcp_oauth import DEFAULT_MCP_SCOPE, SchwabOAuthProvider
from ..config import Settings
from ..risk.guard import RiskGuard
from ..schwab.client import SchwabClient
from .prompts import register_prompts
from .resources import register_resources
from .tools import (
    register_account_tools,
    register_auth_tools,
    register_market_tools,
    register_trading_tools,
)


def _error_html(title: str, detail: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#f8fafc;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}"
        ".box{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:2rem;"
        "max-width:480px;text-align:center}p{color:#cbd5e1}</style></head><body>"
        f"<div class='box'><h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p></div>"
        "</body></html>"
    )


def create_server(
    *,
    settings: Settings | None = None,
    client: SchwabClient | None = None,
    risk: RiskGuard | None = None,
    enable_oauth: bool = False,
    oauth_provider: SchwabOAuthProvider | None = None,
) -> MCPServer:
    """Create and configure the Charles Schwab MCPServer instance.

    Supports both:
    - Mode 1 (Remote Hosted, `enable_oauth=True`): Standards-compliant MCP OAuth 2.1
      Authorization Server proxying Charles Schwab OAuth 2.0 (Cloudflare-style Connect flow).
    - Mode 2 (Local Any-Agent, `enable_oauth=False`): Zero-setup stdio / local server with
      in-chat `start_auth` (auto browser + local HTTPS capture) and `complete_auth`.
    """
    cfg = settings or Settings()
    schwab = client or SchwabClient(cfg)
    guard = risk or RiskGuard(cfg)
    local_callback = LocalCallbackServer(cfg)

    provider: SchwabOAuthProvider | None = None
    auth_settings: AuthSettings | None = None
    if enable_oauth:
        if not cfg.normalized_public_url:
            raise ValueError(
                "MCP_PUBLIC_URL (e.g. https://schwab-mcp.example.com) is required "
                "when running in Mode 1 OAuth 2.1 mode."
            )
        provider = oauth_provider or SchwabOAuthProvider(cfg, schwab.auth)
        auth_settings = AuthSettings(
            issuer_url=AnyHttpUrl(cfg.normalized_public_url),
            resource_server_url=AnyHttpUrl(cfg.resource_server_url),
            validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[DEFAULT_MCP_SCOPE],
                default_scopes=[DEFAULT_MCP_SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[DEFAULT_MCP_SCOPE],
        )

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await local_callback.stop()
            await schwab.aclose()

    mcp = MCPServer(
        name="schwab-mcp",
        title="Charles Schwab Market Data & Trading MCP",
        version=__version__,
        description=(
            "Direct Charles Schwab Market Data v1 & Trader v1 MCP server for equities, ETFs, "
            "and options with OAuth 2.1 remote hosting, smooth local auth, and multi-layer risk controls."
        ),
        instructions=(
            "You have access to Charles Schwab real-time market data, account balances/positions, "
            "and guarded order execution tools. "
            "If any tool returns `SchwabAuthError` with `auth_required=true`, immediately call "
            "`start_auth` to guide the user through quick authentication (or ask them to reconnect "
            "via MCP OAuth). "
            "Before placing any live trade, always inspect `get_risk_config`, verify live quotes/Greeks, "
            "dry-run/preview the order first, and only pass `confirm_live=True` with explicit user approval."
        ),
        auth_server_provider=provider,
        auth=auth_settings,
        lifespan=lifespan,
    )

    register_auth_tools(
        mcp,
        settings=cfg,
        schwab=schwab,
        local_callback=local_callback,
        oauth_enabled=enable_oauth,
    )
    register_market_tools(mcp, schwab=schwab)
    register_account_tools(mcp, schwab=schwab)
    register_trading_tools(mcp, settings=cfg, schwab=schwab, guard=guard)
    register_resources(mcp, settings=cfg, schwab=schwab)
    register_prompts(mcp)

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health_check(_request: Request) -> Response:
        payload: dict[str, object] = {
            "status": "ok",
            "service": "schwab-mcp",
            "version": __version__,
            "mcp_protocol_version": LATEST_PROTOCOL_VERSION,
            "auth_mode": "oauth" if enable_oauth else cfg.effective_http_auth_mode,
            "stateless_http": cfg.mcp_stateless_http,
            "dry_run": cfg.schwab_dry_run,
            "read_only": cfg.schwab_read_only,
        }
        if provider is not None:
            payload["oauth"] = provider.stats()
        return JSONResponse(payload)

    if provider is not None:

        @mcp.custom_route("/schwab/callback", methods=["GET"], include_in_schema=False)
        async def schwab_oauth_callback(request: Request) -> Response:
            params = request.query_params
            try:
                redirect_url = await provider.handle_schwab_callback(
                    code=params.get("code"),
                    state=params.get("state"),
                    error=params.get("error"),
                    error_description=params.get("error_description"),
                )
            except AuthorizeError as exc:
                return HTMLResponse(
                    _error_html(
                        "Charles Schwab Authorization Error",
                        exc.error_description or exc.error,
                    ),
                    status_code=400,
                )
            return RedirectResponse(
                url=redirect_url,
                status_code=302,
                headers={"Cache-Control": "no-store"},
            )

    return mcp
