"""Streamable HTTP ASGI builder supporting Mode 1 OAuth 2.1 and static Bearer auth."""

from __future__ import annotations

import hmac
from typing import Any
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from ..config import Settings


class BearerAuthASGI:
    """Minimal ASGI middleware enforcing `Authorization: Bearer <token>` on HTTP MCP routes.

    Leaves `/health` accessible for container/load-balancer readiness probes.
    """

    def __init__(self, app: Any, expected_token: str) -> None:
        self.app = app
        self.expected_token = expected_token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        prefix = "Bearer "
        provided = auth_header[len(prefix) :] if auth_header.startswith(prefix) else ""
        if not provided or not hmac.compare_digest(provided, self.expected_token):
            response = JSONResponse(
                {"error": "Unauthorized: valid Bearer token required."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _build_transport_security(host: str, public_url: str) -> TransportSecuritySettings | None:
    if host == "0.0.0.0":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if public_url:
        parsed = urlparse(public_url)
        if parsed.netloc:
            return TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[
                    parsed.netloc,
                    "127.0.0.1:*",
                    "localhost:*",
                    "[::1]:*",
                ],
                allowed_origins=[
                    f"{parsed.scheme}://{parsed.netloc}",
                    "http://127.0.0.1:*",
                    "http://localhost:*",
                ],
            )
    return None


def build_http_app(
    mcp: MCPServer,
    *,
    settings: Settings,
    host: str | None = None,
    auth_token: str | None = None,
) -> Any:
    """Build the Starlette/ASGI application for Streamable HTTP transport."""
    bind_host = host or settings.mcp_host
    transport_security = _build_transport_security(bind_host, settings.normalized_public_url)

    app: Any = mcp.streamable_http_app(
        streamable_http_path=settings.normalized_http_path,
        stateless_http=settings.mcp_stateless_http,
        host=bind_host,
        transport_security=transport_security,
    )

    # Static Bearer middleware is only applied when NOT using native MCP OAuth 2.1.
    effective_mode = settings.effective_http_auth_mode
    token = (auth_token if auth_token is not None else settings.mcp_http_auth_token).strip()
    if effective_mode == "bearer" or (effective_mode != "oauth" and token):
        if not token:
            raise ValueError("MCP_AUTH_MODE=bearer requires MCP_HTTP_AUTH_TOKEN to be configured.")
        app = BearerAuthASGI(app, token)

    return app
