"""Real transport-level end-to-end tests (actual stdio subprocess & real TCP Streamable HTTP)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import mcp.types as types
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from schwab_mcp.config import Settings
from schwab_mcp.server.app import create_server
from schwab_mcp.server.http import build_http_app


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_health(url: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient() as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                resp = await client.get(url, timeout=1.0)
                if resp.status_code == 200:
                    data: dict[str, Any] = resp.json()
                    return data
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise TimeoutError(f"Server at {url} did not become healthy within {timeout}s")


async def test_e2e_stdio_subprocess_full_protocol_and_url_elicitation(tmp_path: Path) -> None:
    """Spawn the real `schwab-mcp` process over stdio and exercise tools, resources, prompts,
    and MCP 2026-07-28 URL-mode elicitation using the official MCP ClientSession.
    """
    elicit_events: list[types.ElicitRequestURLParams] = []
    next_action: list[str] = ["accept"]

    async def _elicit_cb(
        _context: Any,
        params: types.ElicitRequestParams,
    ) -> types.ElicitResult:
        assert isinstance(params, types.ElicitRequestURLParams)
        elicit_events.append(params)
        action = next_action[0]
        if action == "accept":
            return types.ElicitResult(action="accept")
        return types.ElicitResult(action="decline")

    env = {
        **os.environ,
        "SCHWAB_DATA_DIR": str(tmp_path / "cfg"),
        "SCHWAB_ENV_FILE": str(tmp_path / "nonexistent.env"),
    }
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "schwab_mcp",
            "--app-key",
            "e2e_key",
            "--app-secret",
            "e2e_secret",
            "--callback-url",
            "https://127.0.0.1:8182",
            "--token-path",
            str(tmp_path / "token.json"),
            "--dry-run",
            "--max-order-usd",
            "3500",
        ],
        env=env,
        cwd=str(tmp_path),
    )

    async with (
        stdio_client(server_params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream, elicitation_callback=_elicit_cb) as session,
    ):
        init_res = await session.initialize()
        assert init_res.server_info.name == "schwab-mcp"

        # 1. Verify full tool catalog + ToolAnnotations
        tools_res = await session.list_tools()
        tools_by_name = {t.name: t for t in tools_res.tools}
        assert len(tools_by_name) == 30
        assert tools_by_name["get_quotes"].annotations is not None
        assert tools_by_name["get_quotes"].annotations.read_only_hint is True
        assert tools_by_name["place_equity_order"].annotations is not None
        assert tools_by_name["place_equity_order"].annotations.destructive_hint is True

        # 2. Verify MCP Resources
        res_list = await session.list_resources()
        uris = {str(r.uri) for r in res_list.resources}
        assert {"schwab://auth/status", "schwab://risk/config", "schwab://accounts"} <= uris

        risk_res = await session.read_resource("schwab://risk/config")
        risk_json = json.loads(risk_res.contents[0].text)  # type: ignore[union-attr]
        assert risk_json["dry_run"] is True
        assert risk_json["max_order_usd"] == 3500.0

        # 3. Verify MCP Prompts
        prompts_res = await session.list_prompts()
        prompt_names = {p.name for p in prompts_res.prompts}
        assert {"safe_order_checklist", "analyze_position", "option_chain_review"} <= prompt_names

        prompt = await session.get_prompt("safe_order_checklist", {"symbol": "NVDA"})
        assert "NVDA" in str(prompt.messages[0].content)

        # 4. Call local helper & status tools over stdio
        occ_res = await session.call_tool(
            "build_option_symbol",
            {
                "underlying": "NVDA",
                "expiration": "2026-12-18",
                "contract_type": "CALL",
                "strike": 150.0,
            },
        )
        occ_data = json.loads(occ_res.content[0].text)  # type: ignore[union-attr]
        assert occ_data["ok"] is True
        assert occ_data["option_symbol"] == "NVDA  261218C00150000"

        # 5. Exercise start_auth -> triggers real MCP URL-mode elicitation!
        auth_res = await session.call_tool(
            "start_auth",
            {"open_browser": False, "auto_capture": False},
        )
        auth_data = json.loads(auth_res.content[0].text)  # type: ignore[union-attr]
        assert auth_data["ok"] is True
        assert auth_data["url_elicitation_action"] == "accept"
        assert len(elicit_events) == 1
        assert elicit_events[0].url.startswith("https://api.schwabapi.com/v1/oauth/authorize?")
        assert "client_id=e2e_key" in elicit_events[0].url

        # 6. Verify declining URL elicitation returns clean declined status
        next_action[0] = "decline"
        declined_res = await session.call_tool(
            "start_auth",
            {"open_browser": False, "auto_capture": False},
        )
        declined_data = json.loads(declined_res.content[0].text)  # type: ignore[union-attr]
        assert declined_data["ok"] is False
        assert declined_data["elicitation_action"] == "decline"


async def test_e2e_real_tcp_streamable_http_and_official_client(tmp_path: Path) -> None:
    """Run an actual uvicorn Streamable HTTP server over TCP and connect with the official
    MCP streamable_http_client + ClientSession (stateless HTTP + Bearer auth).
    """
    port = _find_free_port()
    token = "e2e-bearer-secret"
    settings = Settings(
        schwab_app_key="k",
        schwab_app_secret="s",
        schwab_token_path=tmp_path / "token.json",
        schwab_audit_log_path=tmp_path / "audit.jsonl",
        mcp_auth_mode="bearer",
        mcp_http_auth_token=token,
        mcp_stateless_http=True,
    )
    mcp = create_server(settings=settings, enable_oauth=False)
    app = build_http_app(mcp, settings=settings, host="127.0.0.1")

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    server_task = asyncio.create_task(server.serve())
    try:
        health = await _wait_for_health(f"http://127.0.0.1:{port}/health")
        assert health["status"] == "ok"
        assert health["mcp_protocol_version"] == "2026-07-28"
        assert health["stateless_http"] is True

        # Unauthenticated request must fail with 401
        async with httpx.AsyncClient() as raw_client:
            r_unauth = await raw_client.post(f"http://127.0.0.1:{port}/mcp", json={})
            assert r_unauth.status_code == 401

        # Authenticated official MCP Streamable HTTP client over real TCP
        async with (
            httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http_client,
            streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http_client) as (
                read_stream,
                write_stream,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 30

            risk_call = await session.call_tool("get_risk_config", {})
            risk_data = json.loads(risk_call.content[0].text)  # type: ignore[union-attr]
            assert risk_data["ok"] is True
            assert risk_data["dry_run"] is True
    finally:
        server.should_exit = True
        await server_task


async def test_e2e_real_tcp_mode1_oauth_2_1_discovery_and_dcr(tmp_path: Path) -> None:
    """Run an actual uvicorn Mode 1 OAuth 2.1 server over TCP and verify discovery,
    Dynamic Client Registration, and PKCE /authorize redirect over real sockets.
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    settings = Settings(
        schwab_app_key="mode1_live_key",
        schwab_app_secret="mode1_live_secret",
        schwab_callback_url="",
        mcp_public_url=base_url,
        mcp_auth_mode="oauth",
        schwab_token_path=tmp_path / "token.json",
        schwab_audit_log_path=tmp_path / "audit.jsonl",
        mcp_oauth_state_path=tmp_path / "oauth_state.json",
    )
    mcp = create_server(settings=settings, enable_oauth=True)
    app = build_http_app(mcp, settings=settings, host="127.0.0.1")

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    server_task = asyncio.create_task(server.serve())
    try:
        health = await _wait_for_health(f"{base_url}/health")
        assert health["auth_mode"] == "oauth"
        assert health["mcp_protocol_version"] == "2026-07-28"

        async with httpx.AsyncClient() as client:
            r_401 = await client.post(f"{base_url}/mcp", json={})
            assert r_401.status_code == 401
            assert "resource_metadata=" in r_401.headers["www-authenticate"]

            r_prm = await client.get(f"{base_url}/.well-known/oauth-protected-resource/mcp")
            assert r_prm.status_code == 200
            assert r_prm.json()["resource"] == f"{base_url}/mcp"

            r_reg = await client.post(
                f"{base_url}/register",
                json={
                    "client_name": "E2E TCP Client",
                    "redirect_uris": ["http://127.0.0.1:59999/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            )
            assert r_reg.status_code == 201
            client_id = r_reg.json()["client_id"]

            verifier = secrets.token_urlsafe(48)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                .decode("ascii")
                .rstrip("=")
            )
            r_auth = await client.get(
                f"{base_url}/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": "http://127.0.0.1:59999/callback",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": "tcp_state_1",
                    "resource": f"{base_url}/mcp",
                },
                follow_redirects=False,
            )
            assert r_auth.status_code == 302
            loc = urlparse(r_auth.headers["location"])
            assert loc.netloc == "api.schwabapi.com"
            qs = parse_qs(loc.query)
            assert qs["client_id"] == ["mode1_live_key"]
            assert qs["redirect_uri"] == [f"{base_url}/schwab/callback"]
    finally:
        server.should_exit = True
        await server_task
