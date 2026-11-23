"""Authentication MCP tools (get_auth_status, start_auth with URL elicitation, complete_auth)."""

from __future__ import annotations

import contextlib
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from ...auth.local_callback import LocalCallbackServer, try_open_browser
from ...config import Settings
from ...schwab.client import SchwabClient
from ...schwab.formatters import error_payload


def _client_supports_url_elicitation(ctx: Context[Any, Any] | None) -> bool:
    if ctx is None:
        return False
    try:
        caps = ctx.client_capabilities
        return bool(caps and caps.elicitation and caps.elicitation.url is not None)
    except Exception:
        return False


def register_auth_tools(
    mcp: MCPServer,
    *,
    settings: Settings,
    schwab: SchwabClient,
    local_callback: LocalCallbackServer,
    oauth_enabled: bool = False,
) -> None:
    """Register authentication status and in-chat OAuth tools on the MCP server."""

    @mcp.tool(
        title="Get Schwab Auth Status",
        annotations=ToolAnnotations(
            title="Get Schwab Auth Status",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    async def get_auth_status() -> dict[str, Any]:
        """Check Charles Schwab OAuth 2.0 token health and remaining TTL (30m access & 7d refresh)."""
        status = schwab.auth.get_status()
        status["oauth_mode"] = "mcp_oauth_2_1" if oauth_enabled else "local"
        if not oauth_enabled:
            cb = local_callback.status
            status["local_callback_listener"] = {
                "active": cb.active,
                "listening_url": cb.listening_url,
                "completed": cb.completed,
                "error": cb.error,
            }
        return {"ok": True, **status}

    @mcp.tool(
        title="Start Schwab Authentication",
        annotations=ToolAnnotations(
            title="Start Schwab Authentication",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    async def start_auth(
        ctx: Context[Any, Any],
        open_browser: bool = True,
        auto_capture: bool = True,
        wait_seconds: int = 0,
    ) -> dict[str, Any]:
        """Start Charles Schwab OAuth 2.0 login directly from your AI agent (Mode 2 local flow).

        Supports MCP 2026-07-28 URL-mode elicitation (prompting the client UI natively for
        out-of-band OAuth so credentials never touch the LLM context), plus automatic local
        HTTPS loopback callback capture and manual URL-paste fallback via `complete_auth`.

        Args:
            open_browser: Automatically open the Schwab login URL in your desktop browser when
                client URL-mode elicitation is unavailable (default True).
            auto_capture: Start a temporary local HTTPS listener to capture the callback automatically (default True).
            wait_seconds: Optional seconds to wait for browser completion before returning (0-120, default 0).
        """
        try:
            if oauth_enabled and get_access_token() is not None:
                return {
                    "ok": True,
                    "oauth_mode": "mcp_oauth_2_1",
                    "message": (
                        "This server is running in remote MCP OAuth 2.1 mode and your session "
                        "is already authenticated. To re-authorize after 7 days, reconnect the "
                        "MCP server in your client (Cursor / Codex / Claude)."
                    ),
                    "auth_status": schwab.auth.get_status(),
                }

            state = schwab.auth.issue_local_state()
            auth_url = schwab.auth.build_authorization_url(state=state)
            supports_url_elicit = _client_supports_url_elicitation(ctx)

            capture_status = None
            if auto_capture and settings.is_loopback_callback:

                async def _on_code(code: str) -> None:
                    await schwab.auth.exchange_code(code)
                    schwab.clear_account_cache()
                    if supports_url_elicit:
                        with contextlib.suppress(Exception):
                            await ctx.session.send_elicit_complete(state)

                capture_status = await local_callback.start(
                    expected_state=state,
                    on_code=_on_code,
                    timeout_sec=300,
                )

            url_elicitation_action: str | None = None
            if supports_url_elicit:
                elicit_res = await ctx.elicit_url(
                    message="Sign in to Charles Schwab to authorize schwab-mcp access.",
                    url=auth_url,
                    elicitation_id=state,
                )
                url_elicitation_action = elicit_res.action
                if elicit_res.action in {"decline", "cancel"}:
                    await local_callback.stop()
                    return {
                        "ok": False,
                        "authenticated": False,
                        "elicitation_action": elicit_res.action,
                        "message": f"User {elicit_res.action}d Charles Schwab authorization.",
                    }

            browser_opened = (
                try_open_browser(auth_url) if (open_browser and not supports_url_elicit) else False
            )

            if capture_status and capture_status.active and wait_seconds > 0:
                bounded_wait = max(0, min(int(wait_seconds), 120))
                capture_status = await local_callback.wait_until_complete(bounded_wait)

            completed = bool(capture_status and capture_status.completed)
            result: dict[str, Any] = {
                "ok": True,
                "authenticated": completed,
                "authorization_url": auth_url,
                "callback_url": settings.effective_callback_url,
                "url_elicitation_action": url_elicitation_action,
                "browser_opened": browser_opened,
                "auto_capture_active": bool(capture_status and capture_status.active),
                "listening_url": capture_status.listening_url if capture_status else None,
                "trusted_tls_cert": capture_status.trusted_cert if capture_status else False,
            }
            if capture_status and capture_status.error:
                result["auto_capture_note"] = capture_status.error

            if completed:
                result["message"] = (
                    "Authentication completed automatically! You can now call Schwab market data "
                    "and trading tools."
                )
                result["auth_status"] = schwab.auth.get_status()
            elif capture_status and capture_status.active:
                cert_note = (
                    "Your trusted local TLS certificate is active (no browser warning)."
                    if capture_status.trusted_cert
                    else (
                        "Because Schwab requires HTTPS callbacks, your browser may show a "
                        "local self-signed certificate warning on 127.0.0.1 — either click "
                        "'Advanced -> Proceed' for automatic capture, OR copy the full "
                        "redirected URL from the address bar and send it to `complete_auth`."
                    )
                )
                result["instructions"] = (
                    "1. Open `authorization_url` in your browser and approve access.\n"
                    f"2. Local auto-capture is listening on {capture_status.listening_url}. "
                    f"{cert_note}\n"
                    "3. Once approved, call `get_auth_status` or continue directly with your request."
                )
            else:
                result["instructions"] = (
                    "1. Open `authorization_url` in your browser and sign into Charles Schwab.\n"
                    "2. After approval, copy the FULL redirected URL from your browser address bar "
                    "(e.g. https://127.0.0.1:8182/?code=...&state=...).\n"
                    "3. Send that URL to the agent so it can call `complete_auth`."
                )
            return result
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(
        title="Complete Schwab Authentication",
        annotations=ToolAnnotations(
            title="Complete Schwab Authentication",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def complete_auth(callback_url: str) -> dict[str, Any]:
        """Complete Charles Schwab OAuth 2.0 login by exchanging a callback URL or authorization code.

        Args:
            callback_url: The full redirected URL from your browser address bar
                (e.g. 'https://127.0.0.1:8182/?code=...&session=...') or the raw authorization code.
        """
        try:
            await local_callback.stop()
            code, returned_state = schwab.auth.extract_callback_params(callback_url)
            if (
                returned_state is not None
                and schwab.auth._pending_local_states
                and not schwab.auth.consume_local_state(returned_state)
            ):
                return {
                    "ok": False,
                    "error_type": "SchwabAuthError",
                    "message": "OAuth state mismatch or expired. Please call `start_auth` again.",
                }
            await schwab.auth.exchange_code(code)
            schwab.clear_account_cache()
            return {
                "ok": True,
                "message": "Charles Schwab authentication succeeded and tokens were saved securely (0600).",
                **schwab.auth.get_status(),
            }
        except Exception as exc:
            return error_payload(exc)
