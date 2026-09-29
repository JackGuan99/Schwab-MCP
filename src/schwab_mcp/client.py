"""Thin async client for the read-only parts of the Schwab Trader and Market Data APIs.

Deliberately has no order-placing methods: this project never trades.
Endpoint reference: https://developer.schwab.com (Trader API - Individual).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import date, datetime, timezone
from typing import Any

import httpx

from .auth import TokenManager

logger = logging.getLogger(__name__)

API_BASE = "https://api.schwabapi.com"
TRADER = "/trader/v1"
MARKETDATA = "/marketdata/v1"

TRANSACTION_TYPES = (
    "TRADE",
    "RECEIVE_AND_DELIVER",
    "DIVIDEND_OR_INTEREST",
    "ACH_RECEIPT",
    "ACH_DISBURSEMENT",
    "CASH_RECEIPT",
    "CASH_DISBURSEMENT",
    "ELECTRONIC_FUND",
    "WIRE_OUT",
    "WIRE_IN",
    "JOURNAL",
    "MEMORANDUM",
    "MARGIN_CALL",
    "MONEY_MARKET",
    "SMA_ADJUSTMENT",
)

MAX_RETRIES = 2


class SchwabAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Schwab API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message


def iso_utc(dt: datetime) -> str:
    """Format as Schwab's ``yyyy-MM-dd'T'HH:mm:ss.SSSZ`` (UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def epoch_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _join(values: Iterable[str] | None) -> str | None:
    if values is None:
        return None
    joined = ",".join(v for v in values if v)
    return joined or None


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500] or resp.reason_phrase
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            parts = []
            for err in errors:
                if isinstance(err, dict):
                    parts.append(str(err.get("detail") or err.get("title") or err.get("message") or err))
                else:
                    parts.append(str(err))
            return "; ".join(parts)
        for key in ("message", "error_description", "error"):
            if body.get(key):
                return str(body[key])
    return str(body)[:500]


class SchwabClient:
    def __init__(self, tokens: TokenManager, http: httpx.AsyncClient, base_url: str = API_BASE):
        self.tokens = tokens
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.base_url}{path}"
        refreshed = False
        for attempt in range(MAX_RETRIES + 1):
            token = await self.tokens.get_access_token()
            try:
                resp = await self.http.get(
                    url,
                    params=clean,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except httpx.TimeoutException as exc:
                if attempt < MAX_RETRIES:
                    continue
                raise SchwabAPIError(504, f"request to {path} timed out") from exc
            except httpx.HTTPError as exc:
                raise SchwabAPIError(502, f"could not reach Schwab: {exc}") from exc

            if resp.status_code == 401 and not refreshed:
                refreshed = True
                await self.tokens.force_refresh()
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < MAX_RETRIES:
                delay = _retry_after(resp, default=1.0 + attempt)
                logger.warning("Schwab returned %s for %s; retrying in %.1fs", resp.status_code, path, delay)
                await asyncio.sleep(delay)
                continue
            break

        if resp.is_error:
            raise SchwabAPIError(resp.status_code, _error_message(resp))
        if not resp.content:
            return None
        return resp.json()

    # ----------------------------------------------------------------- accounts

    async def get_account_numbers(self) -> list[dict[str, Any]]:
        """``[{"accountNumber": "12345678", "hashValue": "..."}]``"""
        return await self._get(f"{TRADER}/accounts/accountNumbers") or []

    async def get_accounts(self, *, positions: bool = False) -> list[dict[str, Any]]:
        return await self._get(f"{TRADER}/accounts", {"fields": "positions" if positions else None}) or []

    async def get_account(self, account_hash: str, *, positions: bool = False) -> dict[str, Any]:
        return await self._get(
            f"{TRADER}/accounts/{account_hash}", {"fields": "positions" if positions else None}
        )

    async def get_user_preference(self) -> dict[str, Any]:
        return await self._get(f"{TRADER}/userPreference") or {}

    async def get_orders(
        self,
        account_hash: str | None,
        *,
        from_time: datetime,
        to_time: datetime,
        status: str | None = None,
        max_results: int | None = None,
    ) -> list[dict[str, Any]]:
        path = f"{TRADER}/accounts/{account_hash}/orders" if account_hash else f"{TRADER}/orders"
        params = {
            "fromEnteredTime": iso_utc(from_time),
            "toEnteredTime": iso_utc(to_time),
            "status": status,
            "maxResults": max_results,
        }
        return await self._get(path, params) or []

    async def get_transactions(
        self,
        account_hash: str,
        *,
        start: datetime,
        end: datetime,
        types: Iterable[str] | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {
            "startDate": iso_utc(start),
            "endDate": iso_utc(end),
            "types": _join(types) or ",".join(TRANSACTION_TYPES),
            "symbol": symbol,
        }
        return await self._get(f"{TRADER}/accounts/{account_hash}/transactions", params) or []

    # ------------------------------------------------------------- market data

    async def get_quotes(
        self, symbols: Iterable[str], *, fields: Iterable[str] | None = None, indicative: bool = False
    ) -> dict[str, Any]:
        params = {
            "symbols": _join(symbols),
            "fields": _join(fields),
            "indicative": "true" if indicative else None,
        }
        return await self._get(f"{MARKETDATA}/quotes", params) or {}

    async def get_price_history(
        self,
        symbol: str,
        *,
        period_type: str | None = None,
        period: int | None = None,
        frequency_type: str | None = None,
        frequency: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        extended_hours: bool | None = None,
        previous_close: bool | None = True,
    ) -> dict[str, Any]:
        params = {
            "symbol": symbol,
            "periodType": period_type,
            "period": period,
            "frequencyType": frequency_type,
            "frequency": frequency,
            "startDate": epoch_ms(start) if start else None,
            "endDate": epoch_ms(end) if end else None,
            "needExtendedHoursData": _bool(extended_hours),
            "needPreviousClose": _bool(previous_close),
        }
        return await self._get(f"{MARKETDATA}/pricehistory", params) or {}

    async def get_option_chain(
        self,
        symbol: str,
        *,
        contract_type: str | None = None,
        strike_count: int | None = None,
        strike_range: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        include_underlying_quote: bool = True,
    ) -> dict[str, Any]:
        params = {
            "symbol": symbol,
            "contractType": contract_type,
            "strikeCount": strike_count,
            "range": strike_range,
            "fromDate": from_date.isoformat() if from_date else None,
            "toDate": to_date.isoformat() if to_date else None,
            "includeUnderlyingQuote": _bool(include_underlying_quote),
        }
        return await self._get(f"{MARKETDATA}/chains", params) or {}

    async def get_option_expirations(self, symbol: str) -> dict[str, Any]:
        return await self._get(f"{MARKETDATA}/expirationchain", {"symbol": symbol}) or {}

    async def get_movers(
        self, index: str, *, sort: str | None = None, frequency: int | None = None
    ) -> dict[str, Any]:
        return await self._get(f"{MARKETDATA}/movers/{index}", {"sort": sort, "frequency": frequency}) or {}

    async def get_market_hours(self, markets: Iterable[str], *, on: date | None = None) -> dict[str, Any]:
        params = {"markets": _join(markets), "date": on.isoformat() if on else None}
        return await self._get(f"{MARKETDATA}/markets", params) or {}

    async def get_instruments(self, symbol: str, projection: str) -> dict[str, Any]:
        return (
            await self._get(f"{MARKETDATA}/instruments", {"symbol": symbol, "projection": projection}) or {}
        )


def _bool(value: bool | None) -> str | None:
    return None if value is None else ("true" if value else "false")


def _retry_after(resp: httpx.Response, default: float) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", default)), 10.0)
    except ValueError:
        return default
