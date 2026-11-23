"""Order execution, preview, modification, cancellation, and risk inspection MCP tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ...config import Settings
from ...risk.guard import RiskGuard
from ...schwab.client import SchwabClient
from ...schwab.formatters import error_payload
from ...schwab.orders import (
    build_bracket_order,
    build_equity_order,
    build_exit_oco_order,
    build_occ_symbol,
    build_oco_order,
    build_option_order,
    build_option_spread_order,
    build_trailing_stop_order,
    build_trigger_order,
)

_READ_ONLY_LOCAL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_PREVIEW_ANNOTATION = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
_TRADE_EXECUTION_ANNOTATION = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)


def register_trading_tools(
    mcp: MCPServer,
    *,
    settings: Settings,
    schwab: SchwabClient,
    guard: RiskGuard,
) -> None:
    """Register order helpers, risk config inspection, and guarded trade execution tools."""

    @mcp.tool(title="Get Risk Guardrails Config", annotations=_READ_ONLY_LOCAL)
    async def get_risk_config() -> dict[str, Any]:
        """Inspect active trading risk guardrails (dry_run, read_only, max_order_usd, symbol rules)."""
        return {"ok": True, **settings.risk_summary()}

    @mcp.tool(title="Build OCC Option Symbol", annotations=_READ_ONLY_LOCAL)
    async def build_option_symbol(
        underlying: str,
        expiration: str,
        contract_type: str,
        strike: float,
    ) -> dict[str, Any]:
        """Build a 21-character OCC option symbol required by Charles Schwab.

        Args:
            underlying: Underlying ticker (e.g. 'AAPL', 'SPY').
            expiration: Expiration date in 'YYYY-MM-DD' or 'YYMMDD' format.
            contract_type: 'CALL' (or 'C') or 'PUT' (or 'P').
            strike: Option strike price (e.g. 200.0 or 182.5).
        """
        try:
            occ = build_occ_symbol(underlying, expiration, contract_type, strike)
            return {"ok": True, "option_symbol": occ, "length": len(occ)}
        except Exception as exc:
            return error_payload(exc)

    async def _execute_guarded_order(
        *,
        action_name: str,
        order_payload: dict[str, Any],
        account: str | None,
        dry_run: bool | None,
        confirm_live: bool,
        replace_order_id: str | None = None,
    ) -> dict[str, Any]:
        evaluation = await guard.evaluate_order(
            order_payload,
            request_dry_run=dry_run,
            confirm_live=confirm_live,
            quote_lookup=schwab.get_reference_price,
        )

        if not evaluation.allowed:
            guard.audit(
                f"{action_name}_rejected",
                {
                    "account": account,
                    "reason": evaluation.reason,
                    "estimated_notional_usd": evaluation.estimated_notional_usd,
                    "order_payload": order_payload,
                },
            )
            return {
                "ok": False,
                "blocked_by_risk_guard": True,
                "reason": evaluation.reason,
                "estimated_notional_usd": evaluation.estimated_notional_usd,
                "warnings": evaluation.warnings,
                "order_payload": order_payload,
            }

        acc_num, acc_hash = await schwab.resolve_account_hash(account)

        if evaluation.effective_dry_run:
            preview_resp: Any = None
            preview_error: str | None = None
            try:
                preview_resp = await schwab.preview_order(acc_hash, order_payload)
            except Exception as exc:
                preview_error = str(exc)

            guard.audit(
                f"{action_name}_dry_run",
                {
                    "accountNumber": acc_num,
                    "replace_order_id": replace_order_id,
                    "estimated_notional_usd": evaluation.estimated_notional_usd,
                    "order_payload": order_payload,
                    "preview_error": preview_error,
                },
            )
            return {
                "ok": True,
                "mode": "DRY_RUN",
                "submitted_live": False,
                "accountNumber": acc_num,
                "estimated_notional_usd": evaluation.estimated_notional_usd,
                "warnings": evaluation.warnings,
                "order_payload": order_payload,
                "schwab_preview": preview_resp,
                "preview_error": preview_error,
            }

        if replace_order_id:
            result = await schwab.replace_order(acc_hash, replace_order_id, order_payload)
        else:
            result = await schwab.place_order(acc_hash, order_payload)

        guard.audit(
            f"{action_name}_live",
            {
                "accountNumber": acc_num,
                "replace_order_id": replace_order_id,
                "estimated_notional_usd": evaluation.estimated_notional_usd,
                "order_payload": order_payload,
                "result": result,
            },
        )
        return {
            "ok": True,
            "mode": "LIVE",
            "submitted_live": True,
            "accountNumber": acc_num,
            "estimated_notional_usd": evaluation.estimated_notional_usd,
            "warnings": evaluation.warnings,
            "order_payload": order_payload,
            "result": result,
        }

    @mcp.tool(title="Place Equity / ETF Order", annotations=_TRADE_EXECUTION_ANNOTATION)
    async def place_equity_order(
        symbol: str,
        instruction: str,
        quantity: float,
        order_type: str = "LIMIT",
        price: float | None = None,
        stop_price: float | None = None,
        duration: str = "DAY",
        session: str = "NORMAL",
        account: str | None = None,
        dry_run: bool | None = None,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Place (or dry-run preview) a stock/ETF order with built-in risk checks.

        Args:
            symbol: Equity or ETF ticker (e.g. 'AAPL', 'SPY').
            instruction: 'BUY', 'SELL', 'SELL_SHORT', or 'BUY_TO_COVER'.
            quantity: Number of shares (> 0).
            order_type: 'MARKET', 'LIMIT', 'STOP', 'STOP_LIMIT', 'MARKET_ON_CLOSE', or 'LIMIT_ON_CLOSE'.
            price: Limit price (required for LIMIT, STOP_LIMIT, and LIMIT_ON_CLOSE).
            stop_price: Stop trigger price (required for STOP and STOP_LIMIT).
            duration: 'DAY', 'GOOD_TILL_CANCEL', 'FILL_OR_KILL', or 'IMMEDIATE_OR_CANCEL'.
            session: 'NORMAL', 'AM' (pre-market), 'PM' (after-hours), or 'SEAMLESS' (extended hours require LIMIT+DAY).
            account: Optional account number, last 4 digits, or hashValue.
            dry_run: Force dry-run/preview mode (note: global SCHWAB_DRY_RUN=true cannot be overridden).
            confirm_live: Must be True to submit a real order when SCHWAB_DRY_RUN=false.
        """
        try:
            payload = build_equity_order(
                symbol=symbol,
                instruction=instruction,
                quantity=quantity,
                order_type=order_type,
                price=price,
                stop_price=stop_price,
                duration=duration,
                session=session,
            )
            return await _execute_guarded_order(
                action_name="place_equity_order",
                order_payload=payload,
                account=account,
                dry_run=dry_run,
                confirm_live=confirm_live,
            )
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Place Single-Leg Option Order", annotations=_TRADE_EXECUTION_ANNOTATION)
    async def place_option_order(
        instruction: str,
        quantity: int,
        option_symbol: str | None = None,
        underlying: str | None = None,
        expiration: str | None = None,
        contract_type: str | None = None,
        strike: float | None = None,
        order_type: str = "LIMIT",
        price: float | None = None,
        stop_price: float | None = None,
        duration: str = "DAY",
        account: str | None = None,
        dry_run: bool | None = None,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Place (or dry-run preview) a single-leg option order with built-in risk checks.

        Provide EITHER `option_symbol` (21-char OCC symbol from `get_option_chain`) OR
        all four of (`underlying`, `expiration`, `contract_type`, `strike`).
        """
        try:
            payload = build_option_order(
                instruction=instruction,
                quantity=quantity,
                option_symbol=option_symbol,
                underlying=underlying,
                expiration=expiration,
                contract_type=contract_type,
                strike=strike,
                order_type=order_type,
                price=price,
                stop_price=stop_price,
                duration=duration,
                session="NORMAL",
            )
            return await _execute_guarded_order(
                action_name="place_option_order",
                order_payload=payload,
                account=account,
                dry_run=dry_run,
                confirm_live=confirm_live,
            )
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Place Multi-Leg Option Spread Order", annotations=_TRADE_EXECUTION_ANNOTATION)
    async def place_option_spread_order(
        legs: list[dict[str, Any]],
        order_type: str = "NET_DEBIT",
        price: float | None = None,
        duration: str = "DAY",
        complex_order_strategy_type: str | None = None,
        account: str | None = None,
        dry_run: bool | None = None,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Place (or dry-run preview) a multi-leg option spread or covered strategy order (2-4 legs)."""
        try:
            payload = build_option_spread_order(
                legs=legs,
                order_type=order_type,
                price=price,
                duration=duration,
                session="NORMAL",
                complex_order_strategy_type=complex_order_strategy_type,
            )
            return await _execute_guarded_order(
                action_name="place_option_spread_order",
                order_payload=payload,
                account=account,
                dry_run=dry_run,
                confirm_live=confirm_live,
            )
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Place Trailing Stop Order", annotations=_TRADE_EXECUTION_ANNOTATION)
    async def place_trailing_stop_order(
        instruction: str,
        quantity: float,
        stop_price_offset: float,
        symbol: str | None = None,
        asset_type: str = "EQUITY",
        option_symbol: str | None = None,
        underlying: str | None = None,
        expiration: str | None = None,
        contract_type: str | None = None,
        strike: float | None = None,
        order_type: str = "TRAILING_STOP",
        stop_price_link_type: str = "PERCENT",
        stop_price_link_basis: str = "BID",
        price_offset: float | None = None,
        price_link_type: str = "VALUE",
        price_link_basis: str | None = None,
        duration: str = "GOOD_TILL_CANCEL",
        account: str | None = None,
        dry_run: bool | None = None,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Place (or dry-run preview) a TRAILING_STOP or TRAILING_STOP_LIMIT order for equity or option."""
        try:
            payload = build_trailing_stop_order(
                instruction=instruction,
                quantity=int(quantity) if asset_type.upper() == "OPTION" else quantity,
                stop_price_offset=stop_price_offset,
                symbol=symbol,
                asset_type=asset_type,
                option_symbol=option_symbol,
                underlying=underlying,
                expiration=expiration,
                contract_type=contract_type,
                strike=strike,
                order_type=order_type,
                stop_price_link_type=stop_price_link_type,
                stop_price_link_basis=stop_price_link_basis,
                price_offset=price_offset,
                price_link_type=price_link_type,
                price_link_basis=price_link_basis,
                duration=duration,
                session="NORMAL",
            )
            return await _execute_guarded_order(
                action_name="place_trailing_stop_order",
                order_payload=payload,
                account=account,
                dry_run=dry_run,
                confirm_live=confirm_live,
            )
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Place OCO Exit Order", annotations=_TRADE_EXECUTION_ANNOTATION)
    async def place_oco_order(
        symbol: str | None = None,
        quantity: float | None = None,
        take_profit_price: float | None = None,
        stop_loss_price: float | None = None,
        stop_limit_price: float | None = None,
        trailing_stop_offset: float | None = None,
        trailing_stop_link_type: str = "PERCENT",
        trailing_stop_link_basis: str = "BID",
        asset_type: str = "EQUITY",
        exit_instruction: str | None = None,
        duration: str = "GOOD_TILL_CANCEL",
        child_orders: list[dict[str, Any]] | None = None,
        account: str | None = None,
        dry_run: bool | None = None,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Place (or dry-run preview) an OCO (One-Cancels-Other) order for Take-Profit + Stop-Loss."""
        try:
            if child_orders is not None:
                payload = build_oco_order(child_orders)
            else:
                if not symbol or quantity is None or take_profit_price is None:
                    raise ValueError(
                        "Provide either 'child_orders' (2 raw orders) OR "
                        "('symbol', 'quantity', 'take_profit_price', and 'stop_loss_price' or 'trailing_stop_offset')."
                    )
                payload = build_exit_oco_order(
                    symbol=symbol,
                    quantity=int(quantity) if asset_type.upper() == "OPTION" else quantity,
                    take_profit_price=take_profit_price,
                    asset_type=asset_type,
                    exit_instruction=exit_instruction,
                    stop_loss_price=stop_loss_price,
                    stop_limit_price=stop_limit_price,
                    trailing_stop_offset=trailing_stop_offset,
                    trailing_stop_link_type=trailing_stop_link_type,
                    trailing_stop_link_basis=trailing_stop_link_basis,
                    duration=duration,
                    session="NORMAL",
                )
            return await _execute_guarded_order(
                action_name="place_oco_order",
                order_payload=payload,
                account=account,
                dry_run=dry_run,
                confirm_live=confirm_live,
            )
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Place Bracket Order", annotations=_TRADE_EXECUTION_ANNOTATION)
    async def place_bracket_order(
        symbol: str,
        quantity: float,
        take_profit_price: float,
        entry_order_type: str = "LIMIT",
        entry_price: float | None = None,
        entry_stop_price: float | None = None,
        stop_loss_price: float | None = None,
        stop_limit_price: float | None = None,
        trailing_stop_offset: float | None = None,
        trailing_stop_link_type: str = "PERCENT",
        trailing_stop_link_basis: str = "BID",
        asset_type: str = "EQUITY",
        entry_instruction: str | None = None,
        entry_duration: str = "DAY",
        entry_session: str = "NORMAL",
        exit_duration: str = "GOOD_TILL_CANCEL",
        account: str | None = None,
        dry_run: bool | None = None,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Place (or dry-run preview) a Bracket order (entry triggers OCO Take-Profit + Stop-Loss)."""
        try:
            payload = build_bracket_order(
                symbol=symbol,
                quantity=int(quantity) if asset_type.upper() == "OPTION" else quantity,
                take_profit_price=take_profit_price,
                asset_type=asset_type,
                entry_instruction=entry_instruction,
                entry_order_type=entry_order_type,
                entry_price=entry_price,
                entry_stop_price=entry_stop_price,
                stop_loss_price=stop_loss_price,
                stop_limit_price=stop_limit_price,
                trailing_stop_offset=trailing_stop_offset,
                trailing_stop_link_type=trailing_stop_link_type,
                trailing_stop_link_basis=trailing_stop_link_basis,
                entry_duration=entry_duration,
                entry_session=entry_session,
                exit_duration=exit_duration,
            )
            return await _execute_guarded_order(
                action_name="place_bracket_order",
                order_payload=payload,
                account=account,
                dry_run=dry_run,
                confirm_live=confirm_live,
            )
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Place 1st-Triggers-2nd Order", annotations=_TRADE_EXECUTION_ANNOTATION)
    async def place_trigger_order(
        primary_order: dict[str, Any],
        triggered_orders: list[dict[str, Any]],
        account: str | None = None,
        dry_run: bool | None = None,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Place (or dry-run preview) a 1st-Triggers-2nd (TRIGGER) conditional order."""
        try:
            payload = build_trigger_order(primary_order, triggered_orders)
            return await _execute_guarded_order(
                action_name="place_trigger_order",
                order_payload=payload,
                account=account,
                dry_run=dry_run,
                confirm_live=confirm_live,
            )
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Place Custom Raw Order", annotations=_TRADE_EXECUTION_ANNOTATION)
    async def place_raw_order(
        order_payload: dict[str, Any],
        account: str | None = None,
        dry_run: bool | None = None,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Place (or dry-run preview) any custom Schwab Trader API v1 order JSON payload."""
        try:
            return await _execute_guarded_order(
                action_name="place_raw_order",
                order_payload=order_payload,
                account=account,
                dry_run=dry_run,
                confirm_live=confirm_live,
            )
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Preview Custom Raw Order", annotations=_PREVIEW_ANNOTATION)
    async def preview_raw_order(
        order_payload: dict[str, Any],
        account: str | None = None,
    ) -> dict[str, Any]:
        """Call Schwab's official previewOrder endpoint directly without submitting a live order."""
        try:
            acc_num, acc_hash = await schwab.resolve_account_hash(account)
            preview = await schwab.preview_order(acc_hash, order_payload)
            guard.audit(
                "preview_raw_order",
                {"accountNumber": acc_num, "order_payload": order_payload},
            )
            return {"ok": True, "accountNumber": acc_num, "preview": preview}
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Replace Open Order", annotations=_TRADE_EXECUTION_ANNOTATION)
    async def replace_order(
        order_id: str,
        new_order_payload: dict[str, Any],
        account: str | None = None,
        dry_run: bool | None = None,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Replace/modify an existing open order with a new Schwab order payload."""
        try:
            return await _execute_guarded_order(
                action_name="replace_order",
                order_payload=new_order_payload,
                account=account,
                dry_run=dry_run,
                confirm_live=confirm_live,
                replace_order_id=order_id,
            )
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Cancel Open Order", annotations=_TRADE_EXECUTION_ANNOTATION)
    async def cancel_order(
        order_id: str,
        account: str | None = None,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Cancel an existing open order by order_id (respects SCHWAB_READ_ONLY and SCHWAB_DRY_RUN)."""
        try:
            evaluation = guard.evaluate_cancel(confirm_live=confirm_live)
            if not evaluation.allowed:
                guard.audit(
                    "cancel_order_rejected",
                    {"account": account, "order_id": order_id, "reason": evaluation.reason},
                )
                return {
                    "ok": False,
                    "blocked_by_risk_guard": True,
                    "reason": evaluation.reason,
                }

            acc_num, acc_hash = await schwab.resolve_account_hash(account)
            if evaluation.effective_dry_run:
                guard.audit(
                    "cancel_order_dry_run",
                    {"accountNumber": acc_num, "order_id": order_id},
                )
                return {
                    "ok": True,
                    "mode": "DRY_RUN",
                    "cancelled_live": False,
                    "accountNumber": acc_num,
                    "order_id": order_id,
                    "warnings": evaluation.warnings,
                }

            result = await schwab.cancel_order(acc_hash, order_id)
            guard.audit(
                "cancel_order_live",
                {"accountNumber": acc_num, "order_id": order_id, "result": result},
            )
            return {
                "ok": True,
                "mode": "LIVE",
                "cancelled_live": True,
                "accountNumber": acc_num,
                "result": result,
            }
        except Exception as exc:
            return error_payload(exc)
