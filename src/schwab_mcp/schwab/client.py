"""Async Charles Schwab Market Data v1 & Trader v1 REST API client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote

import httpx

from ..auth.schwab_oauth import SchwabAuthManager
from ..config import Settings
from ..exceptions import SchwabAPIError

MARKETDATA_BASE_URL = "https://api.schwabapi.com/marketdata/v1"
TRADER_BASE_URL = "https://api.schwabapi.com/trader/v1"


def to_epoch_ms(value: str | int | float) -> int:
    """Convert an ISO-8601 date/datetime string or numeric epoch to epoch milliseconds."""
    if isinstance(value, (int, float)):
        num = int(value)
        return num * 1000 if num < 100_000_000_000 else num

    text = value.strip()
    if text.isdigit():
        return to_epoch_ms(int(text))

    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    else:
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def to_schwab_iso_utc(value: str | datetime) -> str:
    """Format a datetime or ISO date string into Schwab Trader API ISO-8601 UTC format."""
    if isinstance(value, datetime):
        dt = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    else:
        text = value.strip()
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
        else:
            iso = text[:-1] + "+00:00" if text.endswith("Z") else text
            dt = datetime.fromisoformat(iso)
            dt = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def extract_order_id_from_location(location: str | None) -> str | None:
    """Extract the order ID from Schwab's 201 Created Location response header."""
    if not location:
        return None
    parts = [p for p in location.rstrip("/").split("/") if p]
    return parts[-1] if parts else None


class SchwabClient:
    """Async REST API client for Charles Schwab Market Data v1 and Trader v1.

    Per-subject account caches ensure strict multi-user isolation when running
    as a shared remote MCP OAuth 2.1 server.
    """

    def __init__(
        self,
        settings: Settings,
        auth: SchwabAuthManager | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=settings.schwab_api_timeout,
            headers={"Accept": "application/json"},
        )
        self.auth = auth or SchwabAuthManager(settings, http_client=self._http)
        self._account_cache: dict[str, list[dict[str, str]]] = {}

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http.aclose()

    def clear_account_cache(self, subject: str | None = None) -> None:
        if subject is None:
            self._account_cache.clear()
        else:
            self._account_cache.pop(subject, None)

    async def _request(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retry_on_401: bool = True,
    ) -> httpx.Response:
        url = f"{base_url}{path}"
        cleaned_params = (
            {
                k: (str(v).lower() if isinstance(v, bool) else v)
                for k, v in params.items()
                if v is not None
            }
            if params
            else None
        )

        access_token = await self.auth.get_valid_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        resp = await self._http.request(
            method,
            url,
            params=cleaned_params,
            json=json_body,
            headers=headers,
        )

        if resp.status_code == 401 and retry_on_401:
            access_token = await self.auth.get_valid_access_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {access_token}"
            resp = await self._http.request(
                method,
                url,
                params=cleaned_params,
                json=json_body,
                headers=headers,
            )

        if resp.status_code >= 400:
            raw_body: Any
            try:
                raw_body = resp.json()
                message = (
                    raw_body.get("message")
                    or raw_body.get("error_description")
                    or raw_body.get("error")
                    or str(raw_body)
                )
            except Exception:
                raw_body = resp.text
                message = resp.text or resp.reason_phrase
            raise SchwabAPIError(resp.status_code, path, message, raw_body=raw_body)

        return resp

    # ==================================================================
    # Market Data API v1
    # ==================================================================

    async def get_quotes(
        self,
        symbols: list[str],
        *,
        fields: str | None = "quote,reference,extended",
        indicative: bool = False,
    ) -> dict[str, Any]:
        cleaned = [s.strip().upper() for s in symbols if s.strip()]
        if not cleaned:
            raise ValueError("At least one ticker symbol is required.")
        resp = await self._request(
            "GET",
            MARKETDATA_BASE_URL,
            "/quotes",
            params={
                "symbols": ",".join(cleaned),
                "fields": fields,
                "indicative": indicative,
            },
        )
        return cast(dict[str, Any], resp.json())

    async def get_reference_price(self, symbol: str) -> float | None:
        """Fetch a live reference price (mark, ask, last, or close) for risk sizing."""
        data = await self.get_quotes([symbol], fields="quote")
        entry = data.get(symbol.upper()) or data.get(symbol)
        if not isinstance(entry, dict):
            entry = next(iter(data.values()), None)
        if not isinstance(entry, dict):
            return None
        q = entry.get("quote", entry)
        for key in ("mark", "askPrice", "lastPrice", "bidPrice", "closePrice"):
            val = q.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return float(val)
        return None

    async def get_price_history(
        self,
        symbol: str,
        *,
        period_type: str | None = "day",
        period: int | None = None,
        frequency_type: str | None = None,
        frequency: int | None = None,
        start_date: str | int | None = None,
        end_date: str | int | None = None,
        need_extended_hours: bool = True,
        need_previous_close: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol.strip().upper(),
            "periodType": period_type,
            "period": period,
            "frequencyType": frequency_type,
            "frequency": frequency,
            "startDate": to_epoch_ms(start_date) if start_date is not None else None,
            "endDate": to_epoch_ms(end_date) if end_date is not None else None,
            "needExtendedHoursData": need_extended_hours,
            "needPreviousClose": need_previous_close,
        }
        resp = await self._request("GET", MARKETDATA_BASE_URL, "/pricehistory", params=params)
        return cast(dict[str, Any], resp.json())

    async def get_option_expirations(self, symbol: str) -> dict[str, Any]:
        resp = await self._request(
            "GET",
            MARKETDATA_BASE_URL,
            "/expirationchain",
            params={"symbol": symbol.strip().upper()},
        )
        return cast(dict[str, Any], resp.json())

    async def get_option_chain(
        self,
        symbol: str,
        *,
        contract_type: str = "ALL",
        strike_count: int | None = 10,
        include_underlying_quote: bool = True,
        strategy: str = "SINGLE",
        interval: float | None = None,
        strike: float | None = None,
        range_filter: str | None = "ALL",
        from_date: str | None = None,
        to_date: str | None = None,
        exp_month: str | None = None,
        option_type: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol.strip().upper(),
            "contractType": contract_type.upper(),
            "strikeCount": strike_count,
            "includeUnderlyingQuote": include_underlying_quote,
            "strategy": strategy.upper(),
            "interval": interval,
            "strike": strike,
            "range": range_filter.upper() if range_filter else None,
            "fromDate": from_date,
            "toDate": to_date,
            "expMonth": exp_month.upper() if exp_month else None,
            "optionType": option_type.upper() if option_type else None,
        }
        resp = await self._request("GET", MARKETDATA_BASE_URL, "/chains", params=params)
        return cast(dict[str, Any], resp.json())

    async def get_market_movers(
        self,
        index: str = "$SPX",
        *,
        sort: str | None = "PERCENT_CHANGE_UP",
        frequency: int | None = 0,
    ) -> dict[str, Any]:
        encoded_index = quote(index.strip(), safe="")
        resp = await self._request(
            "GET",
            MARKETDATA_BASE_URL,
            f"/movers/{encoded_index}",
            params={"sort": sort, "frequency": frequency},
        )
        return cast(dict[str, Any], resp.json())

    async def get_market_hours(
        self,
        markets: str = "equity,option",
        *,
        date: str | None = None,
    ) -> dict[str, Any]:
        resp = await self._request(
            "GET",
            MARKETDATA_BASE_URL,
            "/markets",
            params={"markets": markets, "date": date},
        )
        return cast(dict[str, Any], resp.json())

    async def search_instruments(
        self,
        symbol: str,
        *,
        projection: str = "fundamental",
    ) -> dict[str, Any]:
        resp = await self._request(
            "GET",
            MARKETDATA_BASE_URL,
            "/instruments",
            params={"symbol": symbol.strip(), "projection": projection},
        )
        return cast(dict[str, Any], resp.json())

    # ==================================================================
    # Trader / Accounts API v1
    # ==================================================================

    async def get_account_numbers(self, *, force_refresh: bool = False) -> list[dict[str, str]]:
        subj = self.auth.current_subject()
        if not force_refresh and subj in self._account_cache:
            return self._account_cache[subj]
        resp = await self._request("GET", TRADER_BASE_URL, "/accounts/accountNumbers")
        data: list[dict[str, str]] = resp.json()
        self._account_cache[subj] = data
        return data

    async def resolve_account_hash(self, account: str | None = None) -> tuple[str, str]:
        """Resolve an account selector (hashValue, accountNumber, last 4 digits, or default)."""
        accounts = await self.get_account_numbers()
        if not accounts:
            raise ValueError("No linked Charles Schwab brokerage accounts found.")

        selector = (account or self.settings.schwab_default_account or "").strip()
        if not selector:
            if len(accounts) == 1:
                acc = accounts[0]
                return acc["accountNumber"], acc["hashValue"]
            available = [f"...{a['accountNumber'][-4:]}" for a in accounts]
            raise ValueError(
                f"Multiple Schwab accounts found ({', '.join(available)}). "
                "Specify 'account' (account number, last 4 digits, or hashValue) "
                "or set SCHWAB_DEFAULT_ACCOUNT."
            )

        for acc in accounts:
            if selector == acc["hashValue"] or selector == acc["accountNumber"]:
                return acc["accountNumber"], acc["hashValue"]

        suffix_matches = [a for a in accounts if a["accountNumber"].endswith(selector)]
        if len(suffix_matches) == 1:
            acc = suffix_matches[0]
            return acc["accountNumber"], acc["hashValue"]
        if len(suffix_matches) > 1:
            raise ValueError(
                f"Account selector {selector!r} matches multiple accounts. "
                "Please provide the full account number or hashValue."
            )

        accounts = await self.get_account_numbers(force_refresh=True)
        for acc in accounts:
            if selector == acc["hashValue"] or selector == acc["accountNumber"]:
                return acc["accountNumber"], acc["hashValue"]

        raise ValueError(f"Account {selector!r} not found among linked Schwab accounts.")

    async def get_accounts(self, *, include_positions: bool = True) -> list[dict[str, Any]]:
        params = {"fields": "positions"} if include_positions else None
        resp = await self._request("GET", TRADER_BASE_URL, "/accounts", params=params)
        return cast(list[dict[str, Any]], resp.json())

    async def get_account(
        self, account_hash: str, *, include_positions: bool = True
    ) -> dict[str, Any]:
        params = {"fields": "positions"} if include_positions else None
        resp = await self._request(
            "GET", TRADER_BASE_URL, f"/accounts/{account_hash}", params=params
        )
        return cast(dict[str, Any], resp.json())

    async def get_orders(
        self,
        *,
        account_hash: str | None = None,
        from_entered_time: str | datetime | None = None,
        to_entered_time: str | datetime | None = None,
        days_back: int = 7,
        max_results: int = 50,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        to_dt = to_schwab_iso_utc(to_entered_time) if to_entered_time else to_schwab_iso_utc(now)
        from_dt = (
            to_schwab_iso_utc(from_entered_time)
            if from_entered_time
            else to_schwab_iso_utc(now - timedelta(days=days_back))
        )
        path = f"/accounts/{account_hash}/orders" if account_hash else "/orders"
        resp = await self._request(
            "GET",
            TRADER_BASE_URL,
            path,
            params={
                "fromEnteredTime": from_dt,
                "toEnteredTime": to_dt,
                "maxResults": max_results,
                "status": status.upper() if status else None,
            },
        )
        return cast(list[dict[str, Any]], resp.json())

    async def get_order(self, account_hash: str, order_id: str) -> dict[str, Any]:
        resp = await self._request(
            "GET", TRADER_BASE_URL, f"/accounts/{account_hash}/orders/{order_id}"
        )
        return cast(dict[str, Any], resp.json())

    async def preview_order(
        self, account_hash: str, order_payload: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            TRADER_BASE_URL,
            f"/accounts/{account_hash}/previewOrder",
            json_body=order_payload,
        )
        return (
            cast(dict[str, Any], resp.json()) if resp.content else {"status_code": resp.status_code}
        )

    async def place_order(self, account_hash: str, order_payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            TRADER_BASE_URL,
            f"/accounts/{account_hash}/orders",
            json_body=order_payload,
        )
        location = resp.headers.get("Location") or resp.headers.get("location")
        return {
            "status_code": resp.status_code,
            "order_id": extract_order_id_from_location(location),
            "location": location,
            "body": resp.json() if resp.content else None,
        }

    async def replace_order(
        self, account_hash: str, order_id: str, order_payload: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await self._request(
            "PUT",
            TRADER_BASE_URL,
            f"/accounts/{account_hash}/orders/{order_id}",
            json_body=order_payload,
        )
        location = resp.headers.get("Location") or resp.headers.get("location")
        return {
            "status_code": resp.status_code,
            "replaced_order_id": order_id,
            "new_order_id": extract_order_id_from_location(location),
            "location": location,
            "body": resp.json() if resp.content else None,
        }

    async def cancel_order(self, account_hash: str, order_id: str) -> dict[str, Any]:
        resp = await self._request(
            "DELETE", TRADER_BASE_URL, f"/accounts/{account_hash}/orders/{order_id}"
        )
        return {
            "status_code": resp.status_code,
            "order_id": order_id,
            "cancelled": True,
        }

    async def get_transactions(
        self,
        account_hash: str,
        *,
        start_date: str | datetime | None = None,
        end_date: str | datetime | None = None,
        days_back: int = 30,
        types: str = "TRADE",
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        end_iso = to_schwab_iso_utc(end_date) if end_date else to_schwab_iso_utc(now)
        start_iso = (
            to_schwab_iso_utc(start_date)
            if start_date
            else to_schwab_iso_utc(now - timedelta(days=days_back))
        )
        resp = await self._request(
            "GET",
            TRADER_BASE_URL,
            f"/accounts/{account_hash}/transactions",
            params={
                "startDate": start_iso,
                "endDate": end_iso,
                "types": types.upper(),
                "symbol": symbol.strip().upper() if symbol else None,
            },
        )
        return cast(list[dict[str, Any]], resp.json())

    async def get_user_preferences(self) -> dict[str, Any]:
        resp = await self._request("GET", TRADER_BASE_URL, "/userPreference")
        return cast(dict[str, Any], resp.json())
