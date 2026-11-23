"""Reusable MCP Prompts for safe AI-assisted analysis and order review."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer


def register_prompts(mcp: MCPServer) -> None:
    """Register guided trading & portfolio workflow prompts."""

    @mcp.prompt(
        name="safe_order_checklist",
        title="Safe Order Pre-Flight Checklist",
        description="Step-by-step verification workflow before submitting any live Schwab order.",
    )
    def safe_order_checklist(symbol: str, action: str = "BUY") -> str:
        return (
            f"Before executing a {action.upper()} order on {symbol.upper()}, follow this checklist:\n"
            "1. Call `get_risk_config` to confirm dry-run, read-only, max notional USD, and symbol rules.\n"
            f"2. Call `get_quotes` for `{symbol.upper()}` to verify live bid/ask/mark and spread.\n"
            "3. Call `get_account_details` and `get_positions` to verify buying power and existing exposure.\n"
            "4. Run the intended order tool with `dry_run=True` first to inspect Schwab's official previewOrder "
            "(commission estimate, buying power impact, and warnings).\n"
            "5. Present a clear summary (symbol, side, quantity, order type, limit/stop price, notional USD) "
            "and only pass `confirm_live=True` after explicit user confirmation."
        )

    @mcp.prompt(
        name="analyze_position",
        title="Analyze Portfolio Position & Risk",
        description="Review an existing holding's PnL, live market context, and exit options.",
    )
    def analyze_position(symbol: str) -> str:
        return (
            f"Analyze my Charles Schwab position in {symbol.upper()}:\n"
            "1. Call `get_positions` and `get_orders` to inspect current quantity, cost basis, PnL, and open stops/targets.\n"
            f"2. Call `get_quotes` and `get_price_history` for `{symbol.upper()}` to check recent trend and volatility.\n"
            "3. Summarize unrealized PnL, concentration relative to account liquidation value, and whether "
            "protective OCO or trailing-stop orders are in place."
        )

    @mcp.prompt(
        name="option_chain_review",
        title="Option Chain & Greeks Review",
        description="Inspect expirations, IV, liquidity, and Greeks before selecting an option contract.",
    )
    def option_chain_review(symbol: str, bias: str = "neutral") -> str:
        return (
            f"Review the option chain for {symbol.upper()} with a {bias} outlook:\n"
            f"1. Call `get_quotes` and `get_option_expirations` for `{symbol.upper()}`.\n"
            "2. Call `get_option_chain` for the target expiration range with `compact=True`.\n"
            "3. Compare bid-ask spreads, open interest, implied volatility, delta, and theta, "
            "and highlight liquid candidate strikes with exact 21-character OCC symbols."
        )
