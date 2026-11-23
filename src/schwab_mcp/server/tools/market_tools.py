"""Market Data API v1 MCP tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ...schwab.client import SchwabClient
from ...schwab.formatters import compact_option_chain, compact_quote, error_payload

_READ_ONLY_OPEN = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)


def register_market_tools(mcp: MCPServer, *, schwab: SchwabClient) -> None:
    """Register Charles Schwab Market Data v1 tools on the MCP server."""

    @mcp.tool(title="Get Real-Time Quotes", annotations=_READ_ONLY_OPEN)
    async def get_quotes(
        symbols: list[str] | str,
        fields: str = "quote,reference,extended",
        indicative: bool = False,
        compact: bool = True,
    ) -> dict[str, Any]:
        """Fetch real-time market quotes for one or multiple stocks, ETFs, indexes, or OCC options.

        Args:
            symbols: List of symbols (e.g. ['AAPL', 'NVDA', '$SPX']) or comma-separated string.
            fields: Schwab quote fields (default 'quote,reference,extended').
            indicative: Include indicative quotes for eligible symbols.
            compact: Return concise AI-friendly quote fields (default True) or full Schwab payload.
        """
        try:
            sym_list = (
                [s.strip() for s in symbols.split(",") if s.strip()]
                if isinstance(symbols, str)
                else list(symbols)
            )
            raw = await schwab.get_quotes(sym_list, fields=fields, indicative=indicative)
            if compact:
                quotes = {
                    sym: compact_quote(sym, entry)
                    for sym, entry in raw.items()
                    if isinstance(entry, dict)
                }
                return {"ok": True, "count": len(quotes), "quotes": quotes}
            return {"ok": True, "count": len(raw), "quotes": raw}
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Get Historical Price Bars", annotations=_READ_ONLY_OPEN)
    async def get_price_history(
        symbol: str,
        period_type: str = "day",
        period: int | None = 5,
        frequency_type: str | None = "minute",
        frequency: int | None = 5,
        start_date: str | None = None,
        end_date: str | None = None,
        need_extended_hours: bool = True,
        need_previous_close: bool = True,
        limit: int = 120,
    ) -> dict[str, Any]:
        """Fetch historical OHLCV candlestick price bars for a stock, ETF, or index.

        Args:
            symbol: Ticker symbol (e.g. 'AAPL', 'SPY').
            period_type: 'day', 'month', 'year', or 'ytd'.
            period: Number of periods (day: 1,2,3,4,5,10; month: 1,2,3,6; year: 1,2,3,5,10,15,20; ytd: 1).
                Omit (set None) if specifying start_date/end_date.
            frequency_type: 'minute' (for day), 'daily', 'weekly', or 'monthly'.
            frequency: Candle interval (minute: 1,5,10,15,30; daily/weekly/monthly: 1).
            start_date: Optional start date ('YYYY-MM-DD', ISO-8601 UTC, or epoch ms).
            end_date: Optional end date ('YYYY-MM-DD', ISO-8601 UTC, or epoch ms).
            need_extended_hours: Include pre-market and after-hours candles.
            need_previous_close: Include previous close metadata.
            limit: Maximum number of most-recent candles returned (default 120).
        """
        try:
            effective_period = None if (start_date or end_date) else period
            raw = await schwab.get_price_history(
                symbol,
                period_type=period_type,
                period=effective_period,
                frequency_type=frequency_type,
                frequency=frequency,
                start_date=start_date,
                end_date=end_date,
                need_extended_hours=need_extended_hours,
                need_previous_close=need_previous_close,
            )
            candles = raw.get("candles", [])
            total = len(candles)
            sliced = candles[-limit:] if limit > 0 and total > limit else candles
            formatted = [
                {
                    "time_utc": datetime.fromtimestamp(c["datetime"] / 1000, tz=UTC).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "datetime_ms": c["datetime"],
                    "open": c.get("open"),
                    "high": c.get("high"),
                    "low": c.get("low"),
                    "close": c.get("close"),
                    "volume": c.get("volume"),
                }
                for c in sliced
            ]
            return {
                "ok": True,
                "symbol": raw.get("symbol", symbol.upper()),
                "previousClose": raw.get("previousClose"),
                "total_candles": total,
                "returned_candles": len(formatted),
                "truncated": total > len(formatted),
                "candles": formatted,
            }
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Get Option Expirations", annotations=_READ_ONLY_OPEN)
    async def get_option_expirations(symbol: str) -> dict[str, Any]:
        """List available option expiration dates, DTE, and expiration types for an underlying symbol."""
        try:
            data = await schwab.get_option_expirations(symbol)
            return {"ok": True, "symbol": symbol.upper(), "data": data}
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Get Option Chain", annotations=_READ_ONLY_OPEN)
    async def get_option_chain(
        symbol: str,
        contract_type: str = "ALL",
        strike_count: int = 8,
        range_filter: str = "ALL",
        from_date: str | None = None,
        to_date: str | None = None,
        strike: float | None = None,
        strategy: str = "SINGLE",
        include_underlying_quote: bool = True,
        compact: bool = True,
    ) -> dict[str, Any]:
        """Fetch real-time option chain (calls/puts, strikes, bid/ask, IV, OI, Greeks).

        Args:
            symbol: Underlying ticker (e.g. 'NVDA', 'SPY').
            contract_type: 'ALL', 'CALL', or 'PUT'.
            strike_count: Strikes above and below ATM (default 8).
            range_filter: 'ALL', 'ITM', 'NTM', 'OTM', 'SAK', 'SBK', 'SNK'.
            from_date: Filter expirations from YYYY-MM-DD.
            to_date: Filter expirations up to YYYY-MM-DD.
            strike: Optional single strike price filter.
            strategy: 'SINGLE', 'ANALYTICAL', 'COVERED', 'VERTICAL', 'CALENDAR', 'STRANGLE', 'STRADDLE', etc.
            include_underlying_quote: Include underlying spot quote.
            compact: Return clean contract summaries (default True) or full raw Schwab JSON.
        """
        try:
            raw = await schwab.get_option_chain(
                symbol,
                contract_type=contract_type,
                strike_count=strike_count,
                include_underlying_quote=include_underlying_quote,
                strategy=strategy,
                strike=strike,
                range_filter=range_filter,
                from_date=from_date,
                to_date=to_date,
            )
            return {
                "ok": True,
                "chain": compact_option_chain(raw) if compact else raw,
            }
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Get Market Movers", annotations=_READ_ONLY_OPEN)
    async def get_market_movers(
        index: str = "$SPX",
        sort: str = "PERCENT_CHANGE_UP",
        frequency: int = 0,
    ) -> dict[str, Any]:
        """Fetch top market movers for an index or market segment.

        Args:
            index: '$DJI', '$COMPX', '$SPX', 'NYSE', 'NASDAQ', 'OTCBB', 'INDEX_ALL', 'EQUITY_ALL',
                'OPTION_ALL', 'OPTION_PUT', or 'OPTION_CALL'.
            sort: 'VOLUME', 'TRADES', 'PERCENT_CHANGE_UP', or 'PERCENT_CHANGE_DOWN'.
            frequency: Lookback minutes: 0, 1, 5, 10, 30, or 60.
        """
        try:
            data = await schwab.get_market_movers(index, sort=sort, frequency=frequency)
            return {"ok": True, "index": index, "movers": data}
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Get Market Hours", annotations=_READ_ONLY_OPEN)
    async def get_market_hours(
        markets: str = "equity,option",
        date: str | None = None,
    ) -> dict[str, Any]:
        """Check exchange trading hours and holiday status for specified markets.

        Args:
            markets: Comma-separated list from 'equity', 'option', 'bond', 'future', 'forex'.
            date: Optional YYYY-MM-DD date (defaults to today).
        """
        try:
            data = await schwab.get_market_hours(markets=markets, date=date)
            return {"ok": True, "market_hours": data}
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(title="Search Instruments & Fundamentals", annotations=_READ_ONLY_OPEN)
    async def search_instruments(
        symbol: str,
        projection: str = "fundamental",
    ) -> dict[str, Any]:
        """Search instruments by ticker/description or retrieve fundamental company metrics.

        Args:
            symbol: Ticker or search keyword (e.g. 'AAPL', 'TSLA').
            projection: 'fundamental', 'symbol-search', 'symbol-regex', 'desc-search', 'desc-regex', or 'search'.
        """
        try:
            data = await schwab.search_instruments(symbol, projection=projection)
            return {"ok": True, "instruments": data}
        except Exception as exc:
            return error_payload(exc)
