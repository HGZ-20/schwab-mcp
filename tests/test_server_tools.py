"""End-to-end tests for MCP tools, ToolAnnotations, Resources, Prompts, in-chat auth, and Bearer HTTP."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from mcp.types import LATEST_PROTOCOL_VERSION, PROTOCOL_VERSION_META_KEY
from starlette.testclient import TestClient

from conftest import call_tool_json, seed_valid_token
from schwab_mcp.auth.schwab_oauth import SCHWAB_TOKEN_URL
from schwab_mcp.config import Settings
from schwab_mcp.schwab.client import MARKETDATA_BASE_URL, TRADER_BASE_URL, SchwabClient
from schwab_mcp.server.app import create_server
from schwab_mcp.server.http import build_http_app


@respx.mock
async def test_in_chat_auth_tools_start_and_complete(base_settings: Settings) -> None:
    """Verify Mode 2 in-chat start_auth -> complete_auth flow works without leaving chat."""
    respx.post(SCHWAB_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fresh_access",
                "refresh_token": "fresh_refresh",
                "expires_in": 1800,
                "token_type": "Bearer",
            },
        )
    )

    async with httpx.AsyncClient() as http:
        client = SchwabClient(base_settings, http_client=http)
        mcp = create_server(settings=base_settings, client=client)

        # Unauthenticated call returns actionable auth_required hint
        unauth = await call_tool_json(mcp, "get_quotes", {"symbols": ["AAPL"]})
        assert unauth["ok"] is False
        assert unauth["error_type"] == "SchwabAuthError"
        assert unauth["auth_required"] is True
        assert "start_auth" in unauth["next_step"]

        # Start auth (disable browser + auto_capture for deterministic unit test)
        started = await call_tool_json(
            mcp,
            "start_auth",
            {"open_browser": False, "auto_capture": False},
        )
        assert started["ok"] is True
        assert started["authenticated"] is False
        auth_url = started["authorization_url"]
        state = parse_qs(urlparse(auth_url).query)["state"][0]

        # Complete auth by passing redirected callback URL
        completed = await call_tool_json(
            mcp,
            "complete_auth",
            {"callback_url": f"https://127.0.0.1:8182/?code=C0.abc%40&state={state}"},
        )
        assert completed["ok"] is True
        assert completed["authenticated"] is True


@respx.mock
async def test_tools_annotations_resources_prompts_and_trading(tmp_path: Path) -> None:
    settings = Settings(
        schwab_app_key="k",
        schwab_app_secret="s",
        schwab_token_path=tmp_path / "token.json",
        schwab_audit_log_path=tmp_path / "audit.jsonl",
        schwab_dry_run=False,
        schwab_read_only=False,
        schwab_max_order_usd=5000.0,
    )
    seed_valid_token(settings)

    respx.get(f"{MARKETDATA_BASE_URL}/quotes").mock(
        return_value=httpx.Response(
            200,
            json={
                "AAPL": {
                    "symbol": "AAPL",
                    "assetMainType": "EQUITY",
                    "quote": {"lastPrice": 215.25, "mark": 215.30, "totalVolume": 12345678},
                    "reference": {"description": "Apple Inc."},
                }
            },
        )
    )
    respx.get(f"{MARKETDATA_BASE_URL}/chains").mock(
        return_value=httpx.Response(
            200,
            json={
                "symbol": "AAPL",
                "status": "SUCCESS",
                "underlyingPrice": 215.25,
                "numberOfContracts": 1,
                "callExpDateMap": {
                    "2026-06-18:208": {
                        "220.0": [
                            {
                                "symbol": "AAPL  260618C00220000",
                                "putCall": "CALL",
                                "strikePrice": 220.0,
                                "daysToExpiration": 208,
                                "bid": 12.1,
                                "ask": 12.4,
                                "mark": 12.25,
                                "delta": 0.51,
                            }
                        ]
                    }
                },
                "putExpDateMap": {},
            },
        )
    )
    respx.get(f"{TRADER_BASE_URL}/accounts/accountNumbers").mock(
        return_value=httpx.Response(
            200,
            json=[{"accountNumber": "12345678", "hashValue": "HASH_ABC_123"}],
        )
    )
    respx.get(f"{TRADER_BASE_URL}/accounts/HASH_ABC_123").mock(
        return_value=httpx.Response(
            200,
            json={
                "securitiesAccount": {
                    "accountNumber": "12345678",
                    "type": "MARGIN",
                    "currentBalances": {"liquidationValue": 50000.0, "buyingPower": 24000.0},
                    "positions": [
                        {
                            "longQuantity": 10.0,
                            "shortQuantity": 0.0,
                            "averagePrice": 190.0,
                            "marketValue": 2152.5,
                            "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                        }
                    ],
                }
            },
        )
    )
    respx.post(f"{TRADER_BASE_URL}/accounts/HASH_ABC_123/previewOrder").mock(
        return_value=httpx.Response(
            200,
            json={"orderStrategy": {"status": "PREVIEW"}, "orderBalance": {"orderValue": 2150.0}},
        )
    )
    respx.post(f"{TRADER_BASE_URL}/accounts/HASH_ABC_123/orders").mock(
        return_value=httpx.Response(
            201,
            headers={"Location": f"{TRADER_BASE_URL}/accounts/HASH_ABC_123/orders/99887766"},
        )
    )
    respx.delete(f"{TRADER_BASE_URL}/accounts/HASH_ABC_123/orders/99887766").mock(
        return_value=httpx.Response(200)
    )

    async with httpx.AsyncClient() as http:
        client = SchwabClient(settings, http_client=http)
        mcp = create_server(settings=settings, client=client)

        # Verify ToolAnnotations (MCP 2025-06-18 best practice)
        tools = {t.name: t for t in await mcp.list_tools()}
        assert tools["get_quotes"].annotations is not None
        assert tools["get_quotes"].annotations.read_only_hint is True
        assert tools["place_equity_order"].annotations is not None
        assert tools["place_equity_order"].annotations.destructive_hint is True
        assert {"start_auth", "complete_auth", "get_auth_status"} <= set(tools)

        # Verify MCP Resources & Prompts
        resources = await mcp.list_resources()
        resource_uris = {str(r.uri) for r in resources}
        assert {
            "schwab://auth/status",
            "schwab://risk/config",
            "schwab://accounts",
        } <= resource_uris

        prompts = {p.name for p in await mcp.list_prompts()}
        assert {"safe_order_checklist", "analyze_position", "option_chain_review"} <= prompts

        # Quotes, option chain, positions
        q = await call_tool_json(mcp, "get_quotes", {"symbols": ["AAPL"]})
        assert q["ok"] is True and q["quotes"]["AAPL"]["last"] == 215.25

        chain = await call_tool_json(mcp, "get_option_chain", {"symbol": "AAPL"})
        assert chain["chain"]["calls"]["2026-06-18:208"][0]["symbol"] == "AAPL  260618C00220000"

        pos = await call_tool_json(mcp, "get_positions")
        assert pos["accountNumber"] == "12345678" and pos["positions"][0]["netQuantity"] == 10.0

        # Dry-run, live order, cancel
        dry = await call_tool_json(
            mcp,
            "place_equity_order",
            {
                "symbol": "AAPL",
                "instruction": "BUY",
                "quantity": 10,
                "order_type": "LIMIT",
                "price": 215.0,
                "dry_run": True,
            },
        )
        assert dry["mode"] == "DRY_RUN"

        live = await call_tool_json(
            mcp,
            "place_equity_order",
            {
                "symbol": "AAPL",
                "instruction": "BUY",
                "quantity": 10,
                "order_type": "LIMIT",
                "price": 215.0,
                "confirm_live": True,
            },
        )
        assert live["mode"] == "LIVE" and live["result"]["order_id"] == "99887766"

        cancelled = await call_tool_json(
            mcp,
            "cancel_order",
            {"order_id": "99887766", "confirm_live": True},
        )
        assert cancelled["cancelled_live"] is True
        assert len(settings.schwab_audit_log_path.read_text().strip().splitlines()) == 3


@respx.mock
async def test_401_auto_refresh_retry_and_global_dry_run_guard(base_settings: Settings) -> None:
    seed_valid_token(base_settings)

    respx.post(SCHWAB_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "refreshed_access_token",
                "refresh_token": "valid_refresh_token",
                "expires_in": 1800,
            },
        )
    )
    respx.get(f"{MARKETDATA_BASE_URL}/pricehistory").mock(
        side_effect=[
            httpx.Response(401, json={"message": "Unauthorized"}),
            httpx.Response(
                200,
                json={
                    "symbol": "SPY",
                    "previousClose": 580.0,
                    "candles": [
                        {
                            "datetime": 1763700000000,
                            "open": 581.0,
                            "high": 583.0,
                            "low": 580.5,
                            "close": 582.5,
                            "volume": 100000,
                        }
                    ],
                },
            ),
        ]
    )
    respx.get(f"{TRADER_BASE_URL}/accounts/accountNumbers").mock(
        return_value=httpx.Response(
            200, json=[{"accountNumber": "87654321", "hashValue": "HASH_XYZ"}]
        )
    )
    respx.post(f"{TRADER_BASE_URL}/accounts/HASH_XYZ/previewOrder").mock(
        return_value=httpx.Response(200, json={"orderStrategy": {"status": "PREVIEW"}})
    )

    async with httpx.AsyncClient() as http:
        client = SchwabClient(base_settings, http_client=http)
        mcp = create_server(settings=base_settings, client=client)

        hist = await call_tool_json(
            mcp,
            "get_price_history",
            {"symbol": "SPY", "start_date": "2026-11-01", "end_date": "2026-11-22"},
        )
        assert hist["ok"] is True and hist["candles"][0]["close"] == 582.5

        spread = await call_tool_json(
            mcp,
            "place_option_spread_order",
            {
                "legs": [
                    {
                        "symbol": "SPY   261218C00580000",
                        "instruction": "BUY_TO_OPEN",
                        "quantity": 1,
                    },
                    {
                        "symbol": "SPY   261218C00590000",
                        "instruction": "SELL_TO_OPEN",
                        "quantity": 1,
                    },
                ],
                "order_type": "NET_DEBIT",
                "price": 4.2,
                "complex_order_strategy_type": "VERTICAL",
                "confirm_live": True,
            },
        )
        assert spread["mode"] == "DRY_RUN" and spread["submitted_live"] is False


def test_static_bearer_http_middleware_and_public_health(tmp_path: Path) -> None:
    settings = Settings(
        schwab_token_path=tmp_path / "token.json",
        schwab_audit_log_path=tmp_path / "audit.jsonl",
        mcp_public_url="",
        mcp_auth_mode="bearer",
        mcp_http_auth_token="super-secret-token",
    )
    mcp = create_server(settings=settings, enable_oauth=False)
    app = build_http_app(mcp, settings=settings, host="0.0.0.0")

    with TestClient(app) as tc:
        # /health remains accessible without Bearer auth for container probes
        assert tc.get("/health").status_code == 200

        assert tc.post("/mcp", json={}).status_code == 401
        assert (
            tc.post("/mcp", headers={"Authorization": "Bearer wrong"}, json={}).status_code == 401
        )

        r_disc = tc.post(
            "/mcp",
            headers={
                "Authorization": "Bearer super-secret-token",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
                "MCP-Method": "server/discover",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {
                    "_meta": {
                        PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
        )
        assert r_disc.status_code == 200
        assert "schwab-mcp" in r_disc.text
