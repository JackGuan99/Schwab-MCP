"""The MCP server: read-only Schwab tools for AI agents."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, indicators
from . import formatting as fmt
from .accounts import AccountDirectory, AccountNotFound
from .auth import AuthError, TokenManager, TokenStore
from .client import TRANSACTION_TYPES, SchwabAPIError, SchwabClient
from .config import Settings

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Read-only access to the user's Charles Schwab brokerage accounts and Schwab market data.

- Start with get_portfolio_summary for "how is my portfolio doing" questions, get_positions for holdings
  detail (pass include_quotes=true for live prices), and get_quotes for real-time prices.
- For moving averages, RSI, MACD, volatility and similar, call get_technical_indicators instead of
  computing them from get_price_history candles yourself.
- Accounts are shown as masked labels such as ****1234; pass that label, the last digits, or the
  account nickname wherever a tool takes `account`. Omit `account` to cover all accounts.
- Money is in USD. Timestamps are US/Eastern unless marked UTC. Fields ending in _pct are percents
  (1.5 means 1.5%).
- This server cannot place, change or cancel orders.
- If a tool reports that the Schwab login expired, tell the user to run `schwab-mcp auth` in a terminal.
"""

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
QUOTE_CHUNK = 100
MAX_TECHNICAL_SYMBOLS = 25
HISTORY_CONCURRENCY = 4
# interval -> price-history request that yields enough bars for a 200-period SMA plus a 52-week range
TECHNICAL_HISTORY: dict[str, dict[str, Any]] = {
    "daily": {"period_type": "year", "period": 2, "frequency_type": "daily", "frequency": 1},
    "weekly": {"period_type": "year", "period": 10, "frequency_type": "weekly", "frequency": 1},
}


@dataclass
class AppContext:
    settings: Settings
    tokens: TokenManager
    client: SchwabClient
    accounts: AccountDirectory


def build_context(
    settings: Settings,
    http: httpx.AsyncClient,
    *,
    clock: Callable[[], float] | None = None,
) -> AppContext:
    client_id, client_secret = settings.require_credentials()
    kwargs = {"clock": clock} if clock else {}
    tokens = TokenManager(TokenStore(settings.token_path), client_id, client_secret, http, **kwargs)
    client = SchwabClient(tokens, http)
    return AppContext(settings, tokens, client, AccountDirectory(client, mask=settings.mask_accounts))


@contextmanager
def tool_errors() -> Iterator[None]:
    """Turn expected failures into clean, model-readable tool errors."""
    try:
        yield
    except ToolError:
        raise
    except AuthError as exc:
        raise ToolError(f"Schwab login problem: {exc}") from exc
    except SchwabAPIError as exc:
        hint = ""
        if exc.status_code == 401:
            hint = " Schwab rejected the login; run `schwab-mcp auth` in a terminal."
        elif exc.status_code == 400:
            hint = " Check the arguments (symbols, dates, enum values)."
        elif exc.status_code == 403:
            hint = " Your Schwab app may lack the 'Market Data Production' or 'Accounts and Trading' product."
        elif exc.status_code == 429:
            hint = " Rate limited by Schwab (about 120 requests/minute); wait a moment and retry."
        raise ToolError(f"{exc}.{hint}") from exc
    except (AccountNotFound, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def _as_list(value: list[str] | str | None, *, upper: bool = True) -> list[str]:
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else value
    out = []
    for item in items:
        item = item.strip()
        if item:
            out.append(item.upper() if upper else item)
    return list(dict.fromkeys(out))


def _parse_date(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def _start_of_day_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=fmt.EASTERN).astimezone(timezone.utc)


# Price-history presets: period -> (periodType, period), interval -> (frequencyType, frequency)
PERIODS: dict[str, tuple[str, int]] = {
    "1D": ("day", 1),
    "2D": ("day", 2),
    "3D": ("day", 3),
    "4D": ("day", 4),
    "5D": ("day", 5),
    "10D": ("day", 10),
    "1M": ("month", 1),
    "2M": ("month", 2),
    "3M": ("month", 3),
    "6M": ("month", 6),
    "YTD": ("ytd", 1),
    "1Y": ("year", 1),
    "2Y": ("year", 2),
    "3Y": ("year", 3),
    "5Y": ("year", 5),
    "10Y": ("year", 10),
    "15Y": ("year", 15),
    "20Y": ("year", 20),
}
INTERVALS: dict[str, tuple[str, int]] = {
    "1min": ("minute", 1),
    "5min": ("minute", 5),
    "10min": ("minute", 10),
    "15min": ("minute", 15),
    "30min": ("minute", 30),
    "daily": ("daily", 1),
    "weekly": ("weekly", 1),
    "monthly": ("monthly", 1),
}
ALLOWED_FREQUENCY = {
    "day": {"minute"},
    "month": {"daily", "weekly"},
    "ytd": {"daily", "weekly"},
    "year": {"daily", "weekly", "monthly"},
}

Period = Literal[
    "1D",
    "2D",
    "3D",
    "4D",
    "5D",
    "10D",
    "1M",
    "2M",
    "3M",
    "6M",
    "YTD",
    "1Y",
    "2Y",
    "3Y",
    "5Y",
    "10Y",
    "15Y",
    "20Y",
]
Interval = Literal["1min", "5min", "10min", "15min", "30min", "daily", "weekly", "monthly"]


def default_interval(period: str) -> str:
    period_type, n = PERIODS[period]
    if period_type == "day":
        return "5min" if n == 1 else "30min"
    if period_type in ("month", "ytd"):
        return "daily"
    return "weekly" if n <= 3 else "monthly"


def create_server(
    settings: Settings,
    *,
    context_factory: Callable[[], AbstractAsyncContextManager[AppContext]] | None = None,
) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[AppContext]:
        if context_factory is not None:
            async with context_factory() as ctx:
                yield ctx
            return
        async with httpx.AsyncClient(timeout=settings.request_timeout) as http:
            yield build_context(settings, http)

    mcp = MCPServer(
        "schwab",
        title="Charles Schwab (read-only)",
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    def app(ctx: Context) -> AppContext:
        return ctx.request_context.lifespan_context

    async def public_info(a: AppContext, number: str | int | None) -> dict[str, Any]:
        acct = await a.accounts.by_number(number)
        return acct.public() if acct else {"account": await a.accounts.label_for(number)}

    async def load_accounts(a: AppContext, account: str | None) -> list[dict[str, Any]]:
        """Formatted accounts (with positions) for one account or all of them."""
        if account:
            acct = await a.accounts.resolve(account)
            raws = [await a.client.get_account(acct.hash, positions=True)]
        else:
            raws = await a.client.get_accounts(positions=True)
        out = []
        for raw in raws:
            public = await public_info(a, (raw.get("securitiesAccount") or {}).get("accountNumber"))
            out.append(fmt.format_account(raw, public=public))
        return out

    async def fetch_quotes(
        a: AppContext, symbols: list[str], fields: list[str] | None = None
    ) -> dict[str, Any]:
        chunks = [symbols[i : i + QUOTE_CHUNK] for i in range(0, len(symbols), QUOTE_CHUNK)]
        results = await asyncio.gather(*(a.client.get_quotes(c, fields=fields) for c in chunks))
        merged: dict[str, Any] = {"quotes": {}, "invalid_symbols": []}
        for raw in results:
            part = fmt.format_quotes(raw)
            merged["quotes"].update(part.get("quotes", {}))
            merged["invalid_symbols"] += part.get("invalid_symbols", [])
        return fmt.compact(merged)

    # ------------------------------------------------------------ account tools

    @mcp.tool(annotations=READ_ONLY)
    async def get_auth_status(ctx: Context) -> dict[str, Any]:
        """Check whether the Schwab login is valid and when it expires (Schwab requires a new browser login
        every 7 days). Call this first if other tools report authentication errors."""
        return app(ctx).tokens.status()

    @mcp.tool(annotations=READ_ONLY)
    async def list_accounts(ctx: Context) -> dict[str, Any]:
        """List the linked Schwab accounts with type, nickname and key balances (total value, cash,
        buying power). Use the returned `account` labels with the other tools."""
        a = app(ctx)
        with tool_errors():
            raws = await a.client.get_accounts(positions=False)
            accounts = []
            for raw in raws:
                public = await public_info(a, (raw.get("securitiesAccount") or {}).get("accountNumber"))
                accounts.append(fmt.format_account(raw, public=public, include_positions=False))
            total = sum(x.get("balances", {}).get("total_value", 0) or 0 for x in accounts)
            return await a.accounts.scrub(
                {
                    "as_of": fmt.now_eastern().isoformat(timespec="seconds"),
                    "total_value": fmt.money(total),
                    "accounts": accounts,
                }
            )

    @mcp.tool(annotations=READ_ONLY)
    async def get_positions(
        ctx: Context,
        account: Annotated[
            str | None,
            Field(description="Account label (e.g. ****1234), last digits or nickname. Omit for all."),
        ] = None,
        include_quotes: Annotated[
            bool, Field(description="Also fetch a real-time quote for every holding (adds a `live` block).")
        ] = False,
        symbol: Annotated[str | None, Field(description="Only return positions in this symbol.")] = None,
    ) -> dict[str, Any]:
        """Holdings per account: quantity, average cost, market value, unrealized and today's P/L, and
        weight in the account. Sorted by market value. Set include_quotes=true for live last/mark prices."""
        a = app(ctx)
        with tool_errors():
            accounts = await load_accounts(a, account)
            if symbol:
                wanted = symbol.strip().upper()
                for acct in accounts:
                    acct["positions"] = [
                        p
                        for p in acct.get("positions", [])
                        if wanted in (p.get("symbol"), p.get("underlying"))
                    ]
                    acct["positions_count"] = len(acct["positions"])
            if include_quotes:
                symbols = sorted(
                    {p["symbol"] for x in accounts for p in x.get("positions", []) if p.get("symbol")}
                )
                if symbols:
                    quotes = (await fetch_quotes(a, symbols, ["quote"])).get("quotes", {})
                    for acct in accounts:
                        fmt.attach_quotes(acct.get("positions", []), quotes)
            return await a.accounts.scrub(
                {"as_of": fmt.now_eastern().isoformat(timespec="seconds"), "accounts": accounts}
            )

    @mcp.tool(annotations=READ_ONLY)
    async def get_portfolio_summary(
        ctx: Context,
        top_n: Annotated[int, Field(ge=1, le=50, description="How many top holdings to list.")] = 10,
    ) -> dict[str, Any]:
        """One-shot overview across all accounts: total value, cash, today's P/L, unrealized P/L,
        allocation by asset type, top holdings (merged across accounts) and per-account totals."""
        a = app(ctx)
        with tool_errors():
            accounts = await load_accounts(a, None)
            summary = fmt.portfolio_summary(accounts, top_n=top_n)
            summary = {"as_of": fmt.now_eastern().isoformat(timespec="seconds"), **summary}
            return await a.accounts.scrub(summary)

    @mcp.tool(annotations=READ_ONLY)
    async def get_orders(
        ctx: Context,
        account: Annotated[
            str | None, Field(description="Account label/last digits/nickname. Omit for all.")
        ] = None,
        days: Annotated[int, Field(ge=1, le=60, description="Look back this many days (Schwab max 60).")] = 7,
        status: Annotated[
            Literal[
                "WORKING",
                "FILLED",
                "CANCELED",
                "REJECTED",
                "EXPIRED",
                "PENDING_ACTIVATION",
                "QUEUED",
                "ACCEPTED",
                "AWAITING_PARENT_ORDER",
                "AWAITING_CONDITION",
                "AWAITING_MANUAL_REVIEW",
                "PENDING_CANCEL",
                "PENDING_REPLACE",
                "REPLACED",
                "NEW",
            ]
            | None,
            Field(description="Only orders in this status."),
        ] = None,
        max_results: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        """Recent orders (open, filled, cancelled...) with legs, fills and average fill price. Read-only."""
        a = app(ctx)
        with tool_errors():
            acct = await a.accounts.resolve(account) if account else None
            now = datetime.now(timezone.utc)
            raw = await a.client.get_orders(
                acct.hash if acct else None,
                from_time=now - timedelta(days=days),
                to_time=now,
                status=status,
                max_results=max_results,
            )
            orders = [
                fmt.format_order(o, account_label=await a.accounts.label_for(o.get("accountNumber")))
                for o in raw
            ]
            return await a.accounts.scrub({"count": len(orders), "orders": orders})

    @mcp.tool(annotations=READ_ONLY)
    async def get_transactions(
        ctx: Context,
        account: Annotated[
            str | None, Field(description="Account label/last digits/nickname. Omit for all.")
        ] = None,
        days: Annotated[int, Field(ge=1, le=365, description="Look back this many days (max 365).")] = 30,
        types: Annotated[
            list[Literal[TRANSACTION_TYPES]] | None,  # type: ignore[valid-type]
            Field(description='Transaction types, e.g. ["TRADE", "DIVIDEND_OR_INTEREST"]. Omit for all.'),
        ] = None,
        symbol: Annotated[str | None, Field(description="Only transactions involving this symbol.")] = None,
        max_results: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        """Account history: trades, dividends, interest, deposits/withdrawals and fees, newest first."""
        a = app(ctx)
        with tool_errors():
            targets = [await a.accounts.resolve(account)] if account else await a.accounts.accounts()
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=days)
            results = await asyncio.gather(
                *(
                    a.client.get_transactions(
                        t.hash,
                        start=start,
                        end=end,
                        types=types,
                        symbol=symbol.strip().upper() if symbol else None,
                    )
                    for t in targets
                )
            )
            rows = [
                fmt.format_transaction(tx, account_label=t.label)
                for t, txs in zip(targets, results, strict=True)
                for tx in txs
            ]
            rows.sort(key=lambda r: r.get("time") or "", reverse=True)
            out = {"count": len(rows), "transactions": rows[:max_results]}
            if len(rows) > max_results:
                out["truncated_to"] = max_results
            return await a.accounts.scrub(out)

    # ------------------------------------------------------- market data tools

    @mcp.tool(annotations=READ_ONLY)
    async def get_quotes(
        ctx: Context,
        symbols: Annotated[
            list[str] | str,
            Field(
                description='Ticker symbols, e.g. ["AAPL", "SPY", "$SPX", "/ES"] or "AAPL,MSFT". '
                'Option symbols use the OCC format, e.g. "AAPL  250117C00200000".'
            ),
        ],
        include_fundamentals: Annotated[
            bool, Field(description="Include P/E, EPS, dividend yield and similar fields.")
        ] = True,
    ) -> dict[str, Any]:
        """Real-time quotes: last, bid/ask, day change and %, volume, day range, 52-week range, plus
        greeks for options and optional fundamentals."""
        a = app(ctx)
        with tool_errors():
            syms = _as_list(symbols)
            if not syms:
                raise ValueError("Provide at least one symbol.")
            if len(syms) > 500:
                raise ValueError("At most 500 symbols per call.")
            fields = None if include_fundamentals else ["quote", "reference", "regular"]
            result = await fetch_quotes(a, syms, fields)
            result["as_of"] = fmt.now_eastern().isoformat(timespec="seconds")
            return result

    @mcp.tool(annotations=READ_ONLY)
    async def get_price_history(
        ctx: Context,
        symbol: Annotated[str, Field(description='Ticker, e.g. "AAPL" or "$SPX".')],
        period: Annotated[
            Period, Field(description="Lookback window, ignored when start_date is set.")
        ] = "1M",
        interval: Annotated[
            Interval | None,
            Field(
                description="Candle size. Minute candles only work with day periods (1D-10D) "
                "or with start_date. Default picks a sensible size."
            ),
        ] = None,
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; overrides `period`.")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD (default today).")] = None,
        extended_hours: Annotated[
            bool, Field(description="Include pre/post-market candles (intraday).")
        ] = False,
        max_candles: Annotated[
            int,
            Field(
                ge=0,
                le=2000,
                description="Return only the most recent N candles; "
                "the summary always covers the full range.",
            ),
        ] = 120,
    ) -> dict[str, Any]:
        """OHLCV candles plus a summary (change, % change, high/low, average volume) for a period."""
        a = app(ctx)
        with tool_errors():
            sym = symbol.strip().upper()
            start = _parse_date(start_date, "start_date")
            end = _parse_date(end_date, "end_date")
            interval = interval or ("daily" if start else default_interval(period))
            freq_type, freq = INTERVALS[interval]
            intraday = freq_type == "minute"
            if start:
                period_type, n = ("day", None) if intraday else ("year", None)
                label = {"start_date": start.isoformat(), "end_date": (end or date.today()).isoformat()}
            else:
                period_type, n = PERIODS[period]
                if freq_type not in ALLOWED_FREQUENCY[period_type]:
                    raise ValueError(
                        f"interval '{interval}' is not available for period '{period}'. Allowed: "
                        + ", ".join(
                            k for k, (ft, _) in INTERVALS.items() if ft in ALLOWED_FREQUENCY[period_type]
                        )
                    )
                label = {"period": period}
            label["interval"] = interval
            end_dt = None
            if end:
                end_dt = _start_of_day_utc(end + timedelta(days=1))
            elif start:
                end_dt = datetime.now(timezone.utc)
            raw = await a.client.get_price_history(
                sym,
                period_type=period_type,
                period=n,
                frequency_type=freq_type,
                frequency=freq,
                start=_start_of_day_utc(start) if start else None,
                end=end_dt,
                extended_hours=extended_hours if intraday else None,
            )
            return fmt.format_price_history(raw, intraday=intraday, max_candles=max_candles, label=label)

    @mcp.tool(annotations=READ_ONLY)
    async def get_technical_indicators(
        ctx: Context,
        symbols: Annotated[
            list[str] | str,
            Field(description=f'Up to {MAX_TECHNICAL_SYMBOLS} tickers, e.g. ["AAPL", "SPY"] or "AAPL,MSFT".'),
        ],
        interval: Annotated[
            Literal["daily", "weekly"],
            Field(description="Candle size the indicators are computed on (daily uses 2 years of bars)."),
        ] = "daily",
    ) -> dict[str, Any]:
        """Technical snapshot per symbol, computed server-side from price history: SMA 20/50/200 and
        distance from them, RSI(14), MACD(12,26,9) with last crossover, Bollinger(20,2), ATR(14),
        returns over 1w-1y, realized volatility, 52-week range and golden/death cross state."""
        a = app(ctx)
        with tool_errors():
            syms = _as_list(symbols)
            if not syms:
                raise ValueError("Provide at least one symbol.")
            if len(syms) > MAX_TECHNICAL_SYMBOLS:
                raise ValueError(f"At most {MAX_TECHNICAL_SYMBOLS} symbols per call.")
            gate = asyncio.Semaphore(HISTORY_CONCURRENCY)

            async def one(sym: str) -> dict[str, Any]:
                async with gate:
                    raw = await a.client.get_price_history(sym, **TECHNICAL_HISTORY[interval])
                return indicators.technical_summary(raw, interval=interval)

            results = await asyncio.gather(*(one(s) for s in syms), return_exceptions=True)
            failures = [r for r in results if isinstance(r, BaseException)]
            # Login problems, bugs, or every symbol failing the same way: one clear tool error.
            for exc in failures:
                if not isinstance(exc, SchwabAPIError) or len(failures) == len(syms):
                    raise exc
            out: dict[str, Any] = {"as_of": fmt.now_eastern().isoformat(timespec="seconds"), "indicators": {}}
            errors: dict[str, str] = {}
            for sym, result in zip(syms, results, strict=True):
                if isinstance(result, BaseException):
                    errors[sym] = str(result)
                elif "error" in result:
                    errors[sym] = result["error"]
                else:
                    out["indicators"][sym] = result
            if errors:
                out["errors"] = errors
            return out

    @mcp.tool(annotations=READ_ONLY)
    async def get_option_chain(
        ctx: Context,
        symbol: Annotated[str, Field(description='Underlying ticker, e.g. "AAPL" or "$SPX".')],
        contract_type: Literal["CALL", "PUT", "ALL"] = "ALL",
        strike_count: Annotated[int, Field(ge=1, le=100, description="Strikes around the money.")] = 10,
        from_date: Annotated[str | None, Field(description="Earliest expiration, YYYY-MM-DD.")] = None,
        to_date: Annotated[str | None, Field(description="Latest expiration, YYYY-MM-DD.")] = None,
        max_contracts: Annotated[int, Field(ge=1, le=1000)] = 80,
    ) -> dict[str, Any]:
        """Option chain with bid/ask/mark, volume, open interest, implied volatility and greeks, nearest
        expirations first. Use get_option_expirations to pick dates."""
        a = app(ctx)
        with tool_errors():
            raw = await a.client.get_option_chain(
                symbol.strip().upper(),
                contract_type=contract_type,
                strike_count=strike_count,
                from_date=_parse_date(from_date, "from_date"),
                to_date=_parse_date(to_date, "to_date"),
            )
            return fmt.format_option_chain(raw, max_contracts=max_contracts)

    @mcp.tool(annotations=READ_ONLY)
    async def get_option_expirations(
        ctx: Context, symbol: Annotated[str, Field(description="Underlying ticker.")]
    ) -> dict[str, Any]:
        """All listed option expiration dates for an underlying, with days to expiration."""
        a = app(ctx)
        with tool_errors():
            raw = await a.client.get_option_expirations(symbol.strip().upper())
            return {"symbol": symbol.strip().upper(), "expirations": fmt.format_expirations(raw)}

    @mcp.tool(annotations=READ_ONLY)
    async def get_market_hours(
        ctx: Context,
        markets: list[Literal["equity", "option", "bond", "future", "forex"]] | None = None,
        date: Annotated[str | None, Field(description="YYYY-MM-DD, default today.")] = None,
    ) -> dict[str, Any]:
        """Trading sessions (pre-market, regular, post-market) for a day and whether the market is open.
        For today it also reports which session is active right now (`session_now`)."""
        a = app(ctx)
        with tool_errors():
            on = _parse_date(date, "date")
            raw = await a.client.get_market_hours(markets or ["equity", "option"], on=on)
            now = fmt.now_eastern()
            return {
                "now": now.isoformat(timespec="seconds"),
                "markets": fmt.format_market_hours(raw, now=now),
            }

    @mcp.tool(annotations=READ_ONLY)
    async def get_movers(
        ctx: Context,
        index: Literal[
            "$DJI",
            "$COMPX",
            "$SPX",
            "NYSE",
            "NASDAQ",
            "OTCBB",
            "INDEX_ALL",
            "EQUITY_ALL",
            "OPTION_ALL",
            "OPTION_PUT",
            "OPTION_CALL",
        ] = "$SPX",
        sort: Literal["VOLUME", "TRADES", "PERCENT_CHANGE_UP", "PERCENT_CHANGE_DOWN"] = "PERCENT_CHANGE_UP",
        frequency: Annotated[
            Literal[0, 1, 5, 10, 30, 60], Field(description="Minimum % move to qualify (0 = any).")
        ] = 0,
    ) -> dict[str, Any]:
        """Top movers in an index or exchange, sorted by volume, trades or % change."""
        a = app(ctx)
        with tool_errors():
            raw = await a.client.get_movers(index, sort=sort, frequency=frequency)
            movers = [fmt.format_mover(m) for m in raw.get("screeners") or []]
            return {"index": index, "sort": sort, "movers": movers}

    @mcp.tool(annotations=READ_ONLY)
    async def search_instruments(
        ctx: Context,
        query: Annotated[str, Field(description='Symbol or company-name text, e.g. "NVDA" or "nvidia".')],
        search_type: Annotated[
            Literal["symbol", "description", "fundamental", "symbol_regex", "description_regex"],
            Field(
                description="`description` searches company names; `fundamental` returns P/E, market cap, "
                "beta, margins... for an exact symbol."
            ),
        ] = "symbol",
    ) -> dict[str, Any]:
        """Look up instruments by symbol or company name, or get fundamentals for a symbol."""
        projection = {
            "symbol": "symbol-search",
            "description": "desc-search",
            "fundamental": "fundamental",
            "symbol_regex": "symbol-regex",
            "description_regex": "desc-regex",
        }[search_type]
        a = app(ctx)
        with tool_errors():
            q = query.strip()
            if search_type in ("symbol", "fundamental"):
                q = q.upper()
            raw = await a.client.get_instruments(q, projection)
            items = raw.get("instruments") if isinstance(raw, dict) else raw
            results = [fmt.format_instrument(i) for i in items or []][:50]
            return {"query": q, "count": len(results), "instruments": results}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_current_time() -> dict[str, Any]:
        """Current date and time in US/Eastern (the exchange clock) and UTC. Useful before date math."""
        now = fmt.now_eastern()
        return {
            "eastern": now.isoformat(timespec="seconds"),
            "utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "weekday": now.strftime("%A"),
        }

    # ----------------------------------------------------------------- prompts

    @mcp.prompt()
    def portfolio_review(focus: str = "overall risk, concentration and today's performance") -> str:
        """Review my Schwab portfolio."""
        return (
            "Review my Schwab portfolio. First call get_portfolio_summary, then get_positions with "
            "include_quotes=true. Summarize total value, today's P/L, the biggest winners and losers, "
            f"concentration and cash level. Focus on: {focus}. Use only numbers returned by the tools; "
            "this is information, not investment advice."
        )

    return mcp
