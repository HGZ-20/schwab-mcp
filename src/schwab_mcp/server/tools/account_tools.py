"""Account, portfolio, order query, and transaction MCP tools (Trader API v1)."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ...schwab.client import SchwabClient
from ...schwab.formatters import error_payload, format_account_summary, format_position

_READ_ONLY_OPEN = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)


def register_account_tools(mcp: MCPServer, *, schwab: SchwabClient) -> None:
    """Register Charles Schwab read-only account, portfolio, and order query tools."""

    @mcp.tool(title="List Schwab Accounts", annotations=_READ_ONLY_OPEN)
    async def list_accounts(include_positions: bool = True) -> dict[str, Any]:
        """List all linked Schwab accounts with encrypted hashes, balances, buying power, and positions."""
        try:
            account_numbers = await schwab.get_account_numbers()
            hash_map = {a["accountNumber"]: a["hashValue"] for a in account_numbers}
            raw_accounts = await schwab.get_accounts(include_positions=include_positions)
            formatted = [
                format_account_summary(acc, hash_map, include_positions) for acc in raw_accounts
            ]
            return {"ok": True, "count": len(formatted), "accounts": formatted}
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Get Account Details", annotations=_READ_ONLY_OPEN)
    async def get_account_details(
        account: str | None = None,
        include_positions: bool = True,
        raw: bool = False,
    ) -> dict[str, Any]:
        """Fetch balances, buying power, margin status, and positions for a specific Schwab account.

        Args:
            account: Account number, last 4 digits, or encrypted hashValue (optional if only 1 account or default set).
            include_positions: Include open positions.
            raw: Return full unmodified Schwab JSON payload.
        """
        try:
            acc_num, acc_hash = await schwab.resolve_account_hash(account)
            data = await schwab.get_account(acc_hash, include_positions=include_positions)
            if raw:
                return {
                    "ok": True,
                    "accountNumber": acc_num,
                    "accountHash": acc_hash,
                    "account": data,
                }
            return {
                "ok": True,
                "account": format_account_summary(data, {acc_num: acc_hash}, include_positions),
            }
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Get Open Positions", annotations=_READ_ONLY_OPEN)
    async def get_positions(account: str | None = None) -> dict[str, Any]:
        """Fetch all open equity and option positions for an account with PnL and market values."""
        try:
            acc_num, acc_hash = await schwab.resolve_account_hash(account)
            data = await schwab.get_account(acc_hash, include_positions=True)
            sa = data.get("securitiesAccount", data)
            positions = [format_position(p) for p in sa.get("positions", [])]
            return {
                "ok": True,
                "accountNumber": acc_num,
                "count": len(positions),
                "positions": positions,
            }
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Get Orders", annotations=_READ_ONLY_OPEN)
    async def get_orders(
        account: str | None = None,
        days_back: int = 7,
        from_time: str | None = None,
        to_time: str | None = None,
        status: str | None = None,
        max_results: int = 50,
    ) -> dict[str, Any]:
        """Query orders for a specific account or across all accounts (pass account='ALL').

        Args:
            account: Account number, last 4 digits, hashValue, 'ALL', or None for default account.
            days_back: Lookback window in days when from_time is omitted (default 7, Schwab max 60).
            from_time: Optional ISO-8601 start timestamp (e.g. '2026-11-15T00:00:00Z').
            to_time: Optional ISO-8601 end timestamp.
            status: Optional filter: 'WORKING', 'FILLED', 'CANCELED', 'EXPIRED', 'PENDING_ACTIVATION', 'REJECTED', etc.
            max_results: Maximum orders to return (default 50).
        """
        try:
            acc_hash: str | None = None
            acc_num = "ALL"
            if account and account.strip().upper() == "ALL":
                acc_hash = None
            else:
                acc_num, acc_hash = await schwab.resolve_account_hash(account)

            orders = await schwab.get_orders(
                account_hash=acc_hash,
                from_entered_time=from_time,
                to_entered_time=to_time,
                days_back=days_back,
                max_results=max_results,
                status=status,
            )
            return {
                "ok": True,
                "account": acc_num,
                "count": len(orders),
                "orders": orders,
            }
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Get Order Details", annotations=_READ_ONLY_OPEN)
    async def get_order(order_id: str, account: str | None = None) -> dict[str, Any]:
        """Fetch full details and execution status of a single order by order_id."""
        try:
            acc_num, acc_hash = await schwab.resolve_account_hash(account)
            order = await schwab.get_order(acc_hash, order_id)
            return {"ok": True, "accountNumber": acc_num, "order": order}
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Get Account Transactions", annotations=_READ_ONLY_OPEN)
    async def get_transactions(
        account: str | None = None,
        days_back: int = 30,
        start_date: str | None = None,
        end_date: str | None = None,
        types: str = "TRADE",
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Fetch account transaction history (trades, dividends, option assignments, cash transfers).

        Args:
            account: Account number, last 4 digits, or hashValue.
            days_back: Lookback window in days when start_date is omitted (default 30).
            start_date: Optional start date ('YYYY-MM-DD' or ISO-8601).
            end_date: Optional end date ('YYYY-MM-DD' or ISO-8601).
            types: Schwab transaction type: 'TRADE', 'RECEIVE_AND_DELIVER', 'DIVIDEND_OR_INTEREST',
                'ACH_RECEIPT', 'ACH_DISBURSEMENT', 'CASH_RECEIPT', 'CASH_DISBURSEMENT', 'ELECTRONIC_FUND',
                'WIRE_OUT', 'WIRE_IN', 'JOURNAL', 'MEMORANDUM', 'MARGIN_CALL', 'MONEY_MARKET', 'SMA_ADJUSTMENT'.
            symbol: Optional ticker symbol filter.
        """
        try:
            acc_num, acc_hash = await schwab.resolve_account_hash(account)
            txns = await schwab.get_transactions(
                acc_hash,
                start_date=start_date,
                end_date=end_date,
                days_back=days_back,
                types=types,
                symbol=symbol,
            )
            return {
                "ok": True,
                "accountNumber": acc_num,
                "count": len(txns),
                "transactions": txns,
            }
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Get User Preferences", annotations=_READ_ONLY_OPEN)
    async def get_user_preferences() -> dict[str, Any]:
        """Fetch Schwab user preferences (account nicknames and streamer connection metadata)."""
        try:
            prefs = await schwab.get_user_preferences()
            return {"ok": True, "preferences": prefs}
        except Exception as exc:
            return error_payload(exc)
