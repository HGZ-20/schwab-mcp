"""AI-friendly compact response formatters for Schwab API payloads."""

from __future__ import annotations

from typing import Any

from ..exceptions import SchwabAPIError, SchwabAuthError


def compact_quote(symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Extract concise, LLM-friendly fields from a Schwab quote payload."""
    q = payload.get("quote", {})
    ref = payload.get("reference", {})
    ext = payload.get("extended", {})
    result: dict[str, Any] = {
        "symbol": payload.get("symbol", symbol),
        "assetType": payload.get("assetMainType"),
        "description": ref.get("description"),
        "last": q.get("lastPrice"),
        "mark": q.get("mark"),
        "bid": q.get("bidPrice"),
        "ask": q.get("askPrice"),
        "netChange": q.get("netChange"),
        "netPercentChange": q.get("netPercentChange"),
        "open": q.get("openPrice"),
        "high": q.get("highPrice"),
        "low": q.get("lowPrice"),
        "close": q.get("closePrice"),
        "volume": q.get("totalVolume"),
        "52WeekHigh": q.get("52WeekHigh"),
        "52WeekLow": q.get("52WeekLow"),
        "securityStatus": q.get("securityStatus"),
    }
    if ext:
        result["extended"] = {
            "last": ext.get("lastPrice"),
            "mark": ext.get("mark"),
            "bid": ext.get("bidPrice"),
            "ask": ext.get("askPrice"),
            "volume": ext.get("totalVolume"),
        }
    return {k: v for k, v in result.items() if v is not None}


def compact_option_chain(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract clean option chain contract summaries with Greeks, IV, and OI."""

    def _extract_side(exp_map: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
        if not exp_map:
            return {}
        out: dict[str, list[dict[str, Any]]] = {}
        for exp_key, strikes in exp_map.items():
            contracts: list[dict[str, Any]] = []
            for _strike_str, items in strikes.items():
                for c in items:
                    contracts.append(
                        {
                            "symbol": c.get("symbol"),
                            "putCall": c.get("putCall"),
                            "strike": c.get("strikePrice"),
                            "dte": c.get("daysToExpiration"),
                            "bid": c.get("bid"),
                            "ask": c.get("ask"),
                            "last": c.get("last"),
                            "mark": c.get("mark"),
                            "volume": c.get("totalVolume"),
                            "openInterest": c.get("openInterest"),
                            "iv": c.get("volatility"),
                            "delta": c.get("delta"),
                            "gamma": c.get("gamma"),
                            "theta": c.get("theta"),
                            "vega": c.get("vega"),
                            "inTheMoney": c.get("inTheMoney"),
                        }
                    )
            out[exp_key] = contracts
        return out

    underlying = raw.get("underlying") or {}
    return {
        "symbol": raw.get("symbol"),
        "status": raw.get("status"),
        "underlyingPrice": raw.get("underlyingPrice") or underlying.get("last"),
        "volatility": raw.get("volatility"),
        "numberOfContracts": raw.get("numberOfContracts"),
        "underlying": {
            "last": underlying.get("last"),
            "bid": underlying.get("bid"),
            "ask": underlying.get("ask"),
            "mark": underlying.get("mark"),
            "percentChange": underlying.get("percentChange"),
            "totalVolume": underlying.get("totalVolume"),
        }
        if underlying
        else None,
        "calls": _extract_side(raw.get("callExpDateMap")),
        "puts": _extract_side(raw.get("putExpDateMap")),
    }


def format_position(pos: dict[str, Any]) -> dict[str, Any]:
    """Format a Schwab brokerage position into a concise structure."""
    inst = pos.get("instrument", {})
    long_qty = float(pos.get("longQuantity") or 0)
    short_qty = float(pos.get("shortQuantity") or 0)
    net_qty = long_qty - short_qty
    return {
        "symbol": inst.get("symbol"),
        "assetType": inst.get("assetType"),
        "underlyingSymbol": inst.get("underlyingSymbol"),
        "putCall": inst.get("putCall"),
        "description": inst.get("description"),
        "netQuantity": net_qty,
        "longQuantity": long_qty,
        "shortQuantity": short_qty,
        "averagePrice": pos.get("averagePrice"),
        "marketValue": pos.get("marketValue"),
        "currentDayProfitLoss": pos.get("currentDayProfitLoss"),
        "currentDayProfitLossPercentage": pos.get("currentDayProfitLossPercentage"),
        "longOpenProfitLoss": pos.get("longOpenProfitLoss"),
        "shortOpenProfitLoss": pos.get("shortOpenProfitLoss"),
    }


def format_account_summary(
    raw_account: dict[str, Any],
    hash_map: dict[str, str],
    include_positions: bool,
) -> dict[str, Any]:
    """Format a Schwab securitiesAccount object with balances and optional positions."""
    sa = raw_account.get("securitiesAccount", raw_account)
    acc_num = str(sa.get("accountNumber", ""))
    cur = sa.get("currentBalances", {})
    summary: dict[str, Any] = {
        "accountNumber": acc_num,
        "accountHash": hash_map.get(acc_num),
        "type": sa.get("type"),
        "isDayTrader": sa.get("isDayTrader"),
        "roundTrips": sa.get("roundTrips"),
        "balances": {
            "liquidationValue": cur.get("liquidationValue"),
            "equity": cur.get("equity"),
            "cashBalance": cur.get("cashBalance"),
            "availableFunds": cur.get("availableFunds"),
            "buyingPower": cur.get("buyingPower"),
            "dayTradingBuyingPower": cur.get("dayTradingBuyingPower"),
            "longMarketValue": cur.get("longMarketValue"),
            "shortMarketValue": cur.get("shortMarketValue"),
            "longOptionMarketValue": cur.get("longOptionMarketValue"),
            "shortOptionMarketValue": cur.get("shortOptionMarketValue"),
            "maintenanceRequirement": cur.get("maintenanceRequirement"),
        },
    }
    if include_positions:
        summary["positions"] = [format_position(p) for p in sa.get("positions", [])]
    return summary


def error_payload(exc: Exception) -> dict[str, Any]:
    """Convert exceptions into structured, actionable JSON payloads for AI agents."""
    if isinstance(exc, SchwabAPIError):
        return {
            "ok": False,
            "error_type": "SchwabAPIError",
            "status_code": exc.status_code,
            "endpoint": exc.endpoint,
            "message": exc.message,
            "details": exc.raw_body,
        }
    if isinstance(exc, SchwabAuthError):
        payload: dict[str, Any] = {
            "ok": False,
            "error_type": "SchwabAuthError",
            "auth_required": exc.auth_required,
            "message": str(exc),
        }
        if exc.auth_required:
            payload["next_step"] = (
                "Call the `start_auth` tool to authenticate in-chat, or reconnect via MCP OAuth."
            )
        return payload
    return {
        "ok": False,
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
