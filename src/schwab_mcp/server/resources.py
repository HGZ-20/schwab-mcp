"""MCP Resources exposing live read-only Schwab status and account snapshots."""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from ..config import Settings
from ..schwab.client import SchwabClient
from ..schwab.formatters import format_account_summary


def register_resources(
    mcp: MCPServer,
    *,
    settings: Settings,
    schwab: SchwabClient,
) -> None:
    """Register read-only MCP resources."""

    @mcp.resource(
        "schwab://auth/status",
        name="auth-status",
        title="Charles Schwab OAuth Status",
        description="Current OAuth 2.0 token health, access TTL, and 7-day refresh TTL.",
        mime_type="application/json",
    )
    async def resource_auth_status() -> str:
        return json.dumps(schwab.auth.get_status(), indent=2)

    @mcp.resource(
        "schwab://risk/config",
        name="risk-config",
        title="Active Trading Risk Guardrails",
        description="Active dry-run, read-only, max order notional USD, and symbol rules.",
        mime_type="application/json",
    )
    async def resource_risk_config() -> str:
        return json.dumps(settings.risk_summary(), indent=2)

    @mcp.resource(
        "schwab://accounts",
        name="accounts",
        title="Linked Schwab Brokerage Accounts",
        description="Summary of linked Schwab accounts, buying power, and balances.",
        mime_type="application/json",
    )
    async def resource_accounts() -> str:
        account_numbers = await schwab.get_account_numbers()
        hash_map = {a["accountNumber"]: a["hashValue"] for a in account_numbers}
        raw_accounts = await schwab.get_accounts(include_positions=False)
        formatted = [format_account_summary(acc, hash_map, False) for acc in raw_accounts]
        return json.dumps({"count": len(formatted), "accounts": formatted}, indent=2)
