"""Turn raw Schwab payloads into compact, LLM-friendly dictionaries.

Principles (borrowed from existing Schwab MCP servers and tuned for agents):
- snake_case keys, no nulls, floats rounded - fewer tokens, less confusion;
- derive the numbers an analyst actually asks for (P/L %, weights, allocation);
- timestamps as ISO-8601 in US/Eastern, the exchange's clock;
- everything here is pure and side-effect free, so it is easy to unit test.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

OPTION_MULTIPLIER = 100


# --------------------------------------------------------------------- helpers


def num(value: Any, digits: int = 4) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    r = round(f, digits)
    return int(r) if r.is_integer() and abs(r) < 1e15 else r


def money(value: Any) -> float | int | None:
    return num(value, 2)


def pct(numerator: Any, denominator: Any) -> float | None:
    n, d = num(numerator, 10), num(denominator, 10)
    if n is None or not d:
        return None
    return round(n / abs(d) * 100, 2)


def ms_to_iso(ms: Any, *, date_only: bool = False) -> str | None:
    if ms is None or isinstance(ms, bool):
        return None
    try:
        dt = datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).astimezone(EASTERN)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return dt.date().isoformat() if date_only else dt.isoformat(timespec="seconds")


def compact(value: Any) -> Any:
    """Drop None / empty containers recursively."""
    if isinstance(value, dict):
        out = {k: compact(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v is not None and v != {} and v != []}
    if isinstance(value, list):
        return [compact(v) for v in value if v is not None]
    return value


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def snake(key: str) -> str:
    return _CAMEL.sub("_", key).lower()


def now_eastern() -> datetime:
    return datetime.now(tz=EASTERN)


# -------------------------------------------------------------------- balances

_BALANCE_FIELDS = {
    "liquidationValue": "total_value",
    "cashBalance": "cash_balance",
    "totalCash": "total_cash",
    "moneyMarketFund": "money_market_fund",
    "cashAvailableForTrading": "cash_available_for_trading",
    "cashAvailableForWithdrawal": "cash_available_for_withdrawal",
    "unsettledCash": "unsettled_cash",
    "longMarketValue": "long_market_value",
    "shortMarketValue": "short_market_value",
    "longOptionMarketValue": "long_option_market_value",
    "shortOptionMarketValue": "short_option_market_value",
    "mutualFundValue": "mutual_fund_value",
    "bondValue": "bond_value",
    "buyingPower": "buying_power",
    "dayTradingBuyingPower": "day_trading_buying_power",
    "availableFunds": "available_funds",
    "equity": "equity",
    "marginBalance": "margin_balance",
    "maintenanceRequirement": "maintenance_requirement",
    "maintenanceCall": "maintenance_call",
    "regTCall": "reg_t_call",
    "pendingDeposits": "pending_deposits",
}


def format_balances(balances: dict[str, Any] | None) -> dict[str, Any]:
    balances = balances or {}
    out = {}
    for src, dst in _BALANCE_FIELDS.items():
        value = money(balances.get(src))
        if value is not None and (value != 0 or src in ("liquidationValue", "cashBalance")):
            out[dst] = value
    return out


def total_value(securities_account: dict[str, Any]) -> float:
    for key in ("currentBalances", "initialBalances"):
        value = num((securities_account.get(key) or {}).get("liquidationValue"), 10)
        if value is not None:
            return float(value)
    return float(sum(num(p.get("marketValue"), 10) or 0 for p in securities_account.get("positions") or []))


# ------------------------------------------------------------------- positions


def format_position(p: dict[str, Any], *, account_value: float | None = None) -> dict[str, Any]:
    inst = p.get("instrument") or {}
    asset_type = inst.get("assetType")
    long_qty = num(p.get("longQuantity")) or 0
    short_qty = num(p.get("shortQuantity")) or 0
    is_short = short_qty > 0 and not long_qty
    quantity = -short_qty if is_short else long_qty

    if is_short:
        avg_price = p.get("averageShortPrice", p.get("averagePrice"))
        unrealized = p.get("shortOpenProfitLoss")
    else:
        avg_price = p.get("averageLongPrice", p.get("averagePrice"))
        unrealized = p.get("longOpenProfitLoss")

    market_value = num(p.get("marketValue"), 10)
    cost_basis = None
    if market_value is not None and unrealized is not None:
        cost_basis = market_value - float(unrealized)

    multiplier = OPTION_MULTIPLIER if asset_type == "OPTION" else 1
    price = None
    if (
        market_value is not None
        and quantity
        and asset_type in ("EQUITY", "COLLECTIVE_INVESTMENT", "OPTION", "MUTUAL_FUND")
    ):
        price = market_value / (quantity * multiplier)

    out: dict[str, Any] = {
        "symbol": inst.get("symbol"),
        "description": inst.get("description"),
        "asset_type": asset_type,
        "sub_type": inst.get("type"),
        "quantity": num(quantity),
        "side": "SHORT" if is_short else "LONG",
        "average_price": num(avg_price),
        "price": num(price),
        "market_value": money(market_value),
        "cost_basis": money(cost_basis),
        "unrealized_pl": money(unrealized),
        "unrealized_pl_pct": pct(unrealized, cost_basis),
        "day_pl": money(p.get("currentDayProfitLoss")),
        "day_pl_pct": num(p.get("currentDayProfitLossPercentage"), 2),
        "weight_pct": pct(market_value, account_value) if account_value else None,
    }
    if asset_type == "OPTION":
        out["underlying"] = inst.get("underlyingSymbol")
        out["put_call"] = inst.get("putCall")
    return compact(out)


def _sort_key(pos: dict[str, Any]) -> float:
    return -abs(pos.get("market_value") or 0)


def format_account(
    raw: dict[str, Any], *, public: dict[str, Any], include_positions: bool = True
) -> dict[str, Any]:
    sa = raw.get("securitiesAccount") or raw
    value = total_value(sa)
    out: dict[str, Any] = dict(public)
    out["type"] = sa.get("type") or out.get("type")  # MARGIN / CASH beats the generic BROKERAGE
    if sa.get("isDayTrader"):
        out["pattern_day_trader"] = True
    if sa.get("roundTrips"):
        out["round_trips"] = sa.get("roundTrips")
    out["balances"] = format_balances(sa.get("currentBalances") or sa.get("initialBalances"))
    if include_positions:
        positions = [format_position(p, account_value=value) for p in sa.get("positions") or []]
        positions.sort(key=_sort_key)
        out["positions_count"] = len(positions)
        out["positions"] = positions
    return compact(out)


def attach_quotes(positions: list[dict[str, Any]], quotes: dict[str, dict[str, Any]]) -> None:
    """Enrich formatted positions (in place) with fields from :func:`format_quote`."""
    for pos in positions:
        q = quotes.get(pos.get("symbol") or "")
        if not q:
            continue
        pos["live"] = compact(
            {
                "last": q.get("last"),
                "mark": q.get("mark"),
                "net_change": q.get("net_change"),
                "net_change_pct": q.get("net_change_pct"),
                "quote_time": q.get("quote_time"),
            }
        )


def portfolio_summary(accounts: list[dict[str, Any]], *, top_n: int = 10) -> dict[str, Any]:
    """Aggregate already-formatted accounts (from :func:`format_account`)."""
    total = sum(a.get("balances", {}).get("total_value", 0) or 0 for a in accounts)
    cash = sum(
        (a.get("balances", {}).get("cash_balance", 0) or 0)
        + (a.get("balances", {}).get("money_market_fund", 0) or 0)
        for a in accounts
    )
    day_pl = 0.0
    unrealized = 0.0
    by_type: dict[str, float] = defaultdict(float)
    by_symbol: dict[str, dict[str, Any]] = {}
    for acct in accounts:
        for pos in acct.get("positions", []):
            mv = pos.get("market_value") or 0
            day_pl += pos.get("day_pl") or 0
            unrealized += pos.get("unrealized_pl") or 0
            by_type[pos.get("asset_type") or "OTHER"] += mv
            sym = pos.get("symbol") or "?"
            agg = by_symbol.setdefault(
                sym,
                {
                    "symbol": sym,
                    "description": pos.get("description"),
                    "asset_type": pos.get("asset_type"),
                    "quantity": 0,
                    "market_value": 0.0,
                    "unrealized_pl": 0.0,
                    "day_pl": 0.0,
                    "accounts": [],
                },
            )
            agg["quantity"] += pos.get("quantity") or 0
            agg["market_value"] += mv
            agg["unrealized_pl"] += pos.get("unrealized_pl") or 0
            agg["day_pl"] += pos.get("day_pl") or 0
            agg["accounts"].append(acct.get("account"))

    if cash:
        by_type["CASH_AND_SWEEP"] += cash
    allocation = [
        {"asset_type": k, "market_value": money(v), "pct": pct(v, total)}
        for k, v in sorted(by_type.items(), key=lambda kv: -abs(kv[1]))
    ]
    holdings = sorted(by_symbol.values(), key=lambda h: -abs(h["market_value"]))
    top = []
    for h in holdings[:top_n]:
        top.append(
            compact(
                {
                    **h,
                    "quantity": num(h["quantity"]),
                    "market_value": money(h["market_value"]),
                    "unrealized_pl": money(h["unrealized_pl"]),
                    "day_pl": money(h["day_pl"]),
                    "weight_pct": pct(h["market_value"], total),
                    "accounts": sorted(set(filter(None, h["accounts"]))),
                }
            )
        )
    return compact(
        {
            "total_value": money(total),
            "cash_and_sweep": money(cash),
            "invested_value": money(total - cash),
            "day_pl": money(day_pl),
            "day_pl_pct": pct(day_pl, total - day_pl) if total else None,
            "unrealized_pl": money(unrealized),
            "positions_count": sum(a.get("positions_count", 0) for a in accounts),
            "distinct_holdings": len(by_symbol),
            "allocation_by_asset_type": allocation,
            "top_holdings": top,
            "accounts": [
                compact(
                    {
                        "account": a.get("account"),
                        "nickname": a.get("nickname"),
                        "type": a.get("type"),
                        "total_value": a.get("balances", {}).get("total_value"),
                        "pct_of_total": pct(a.get("balances", {}).get("total_value"), total),
                    }
                )
                for a in accounts
            ],
        }
    )


# ---------------------------------------------------------------------- quotes

_QUOTE_FIELDS = {
    "lastPrice": "last",
    "mark": "mark",
    "bidPrice": "bid",
    "askPrice": "ask",
    "bidSize": "bid_size",
    "askSize": "ask_size",
    "openPrice": "open",
    "highPrice": "high",
    "lowPrice": "low",
    "closePrice": "previous_close",
    "netChange": "net_change",
    "netPercentChange": "net_change_pct",
    "totalVolume": "volume",
    "52WeekHigh": "high_52w",
    "52WeekLow": "low_52w",
    "postMarketChange": "post_market_change",
    "postMarketPercentChange": "post_market_change_pct",
    "nAV": "nav",
    "securityStatus": "security_status",
    # option-only
    "underlyingPrice": "underlying_price",
    "strikePrice": "strike",
    "openInterest": "open_interest",
    "volatility": "implied_volatility",
    "delta": "delta",
    "gamma": "gamma",
    "theta": "theta",
    "vega": "vega",
    "rho": "rho",
    "theoreticalOptionValue": "theoretical_value",
    "timeValue": "time_value",
    "daysToExpiration": "days_to_expiration",
    "moneyIntrinsicValue": "intrinsic_value",
}

_FUNDAMENTAL_FIELDS = {
    "peRatio": "pe_ratio",
    "eps": "eps",
    "divYield": "dividend_yield_pct",
    "divAmount": "dividend_amount",
    "divExDate": "ex_dividend_date",
    "nextDivExDate": "next_ex_dividend_date",
    "nextDivPayDate": "next_dividend_pay_date",
    "avg10DaysVolume": "avg_volume_10d",
    "avg1YearVolume": "avg_volume_1y",
    "lastEarningsDate": "last_earnings_date",
}


def format_quote(symbol: str, raw: dict[str, Any]) -> dict[str, Any]:
    quote = raw.get("quote") or {}
    ref = raw.get("reference") or {}
    fund = raw.get("fundamental") or {}
    regular = raw.get("regular") or {}

    out: dict[str, Any] = {
        "symbol": raw.get("symbol") or symbol,
        "description": ref.get("description"),
        "asset_type": raw.get("assetMainType"),
        "exchange": ref.get("exchangeName"),
        "realtime": raw.get("realtime"),
    }
    for src, dst in _QUOTE_FIELDS.items():
        if src in quote:
            out[dst] = quote[src] if isinstance(quote[src], str) else num(quote[src])
    if "expirationDate" in ref or "contractType" in ref:
        out["option"] = compact(
            {
                "underlying": ref.get("underlying"),
                "put_call": {"C": "CALL", "P": "PUT"}.get(ref.get("contractType"), ref.get("contractType")),
                "strike": num(ref.get("strikePrice")),
                "expiration": _date_part(ref.get("expirationDate")),
                "multiplier": num(ref.get("multiplier")),
            }
        )
    out["quote_time"] = ms_to_iso(quote.get("quoteTime"))
    out["trade_time"] = ms_to_iso(quote.get("tradeTime"))
    if regular:
        out["regular_session"] = compact(
            {
                "last": num(regular.get("regularMarketLastPrice")),
                "net_change": num(regular.get("regularMarketNetChange")),
                "net_change_pct": num(regular.get("regularMarketPercentChange")),
                "trade_time": ms_to_iso(regular.get("regularMarketTradeTime")),
            }
        )
    if fund:
        out["fundamental"] = compact(
            {
                dst: (_date_part(fund[src]) if src.endswith("Date") else num(fund[src]))
                for src, dst in _FUNDAMENTAL_FIELDS.items()
                if src in fund and fund[src] not in (0, "", None)
            }
        )
    return compact(out)


def format_quotes(raw: dict[str, Any]) -> dict[str, Any]:
    quotes = {}
    invalid: list[str] = []
    for key, value in (raw or {}).items():
        if key == "errors" and isinstance(value, dict):
            for bucket in value.values():
                if isinstance(bucket, list):
                    invalid.extend(str(v) for v in bucket)
            continue
        if isinstance(value, dict) and "invalidSymbols" in value:
            invalid.extend(value["invalidSymbols"])
            continue
        if isinstance(value, dict):
            quotes[key] = format_quote(key, value)
    return compact({"quotes": quotes, "invalid_symbols": sorted(set(invalid)) or None})


def _date_part(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
        return value[:10]
    return value


# --------------------------------------------------------------- price history


def format_price_history(
    raw: dict[str, Any], *, intraday: bool, max_candles: int, label: dict[str, Any]
) -> dict[str, Any]:
    candles = raw.get("candles") or []
    out: dict[str, Any] = {"symbol": raw.get("symbol"), **label, "candles_total": len(candles)}
    if candles:
        first, last = candles[0], candles[-1]
        highs = [c.get("high") for c in candles if c.get("high") is not None]
        lows = [c.get("low") for c in candles if c.get("low") is not None]
        volumes = [c.get("volume") for c in candles if c.get("volume") is not None]
        if label.get("period") == "1D" and raw.get("previousClose"):
            basis, base = "previous_close", raw.get("previousClose")
        else:
            basis, base = "first_open", first.get("open")
        change = (last.get("close") or 0) - base if base and last.get("close") is not None else None
        out["summary"] = compact(
            {
                "start": ms_to_iso(first.get("datetime"), date_only=not intraday),
                "end": ms_to_iso(last.get("datetime"), date_only=not intraday),
                "open": num(first.get("open")),
                "close": num(last.get("close")),
                "change": num(change),
                "change_pct": pct(change, base),
                "change_basis": basis if change is not None else None,
                "high": num(max(highs)) if highs else None,
                "low": num(min(lows)) if lows else None,
                "avg_volume": num(sum(volumes) / len(volumes), 0) if volumes else None,
            }
        )
    if raw.get("previousClose") is not None:
        out["previous_close"] = num(raw.get("previousClose"))
    shown = candles[-max_candles:] if max_candles > 0 else []
    if len(shown) < len(candles):
        out["truncated_to_last"] = len(shown)
    out["candles"] = [
        compact(
            {
                "t": ms_to_iso(c.get("datetime"), date_only=not intraday),
                "o": num(c.get("open")),
                "h": num(c.get("high")),
                "l": num(c.get("low")),
                "c": num(c.get("close")),
                "v": num(c.get("volume")),
            }
        )
        for c in shown
    ]
    return compact(out)


# ---------------------------------------------------------------------- options


def format_option_chain(raw: dict[str, Any], *, max_contracts: int) -> dict[str, Any]:
    contracts: list[dict[str, Any]] = []
    for map_key in ("callExpDateMap", "putExpDateMap"):
        for exp_key, strikes in (raw.get(map_key) or {}).items():
            for _strike, items in (strikes or {}).items():
                for c in items or []:
                    contracts.append(
                        compact(
                            {
                                "symbol": c.get("symbol"),
                                "put_call": c.get("putCall"),
                                "expiration": _date_part(c.get("expirationDate")) or exp_key.split(":")[0],
                                "dte": c.get("daysToExpiration"),
                                "strike": num(c.get("strikePrice")),
                                "bid": num(c.get("bid")),
                                "ask": num(c.get("ask")),
                                "mark": num(c.get("mark")),
                                "last": num(c.get("last")),
                                "volume": num(c.get("totalVolume")),
                                "open_interest": num(c.get("openInterest")),
                                "iv": num(c.get("volatility"), 2),
                                "delta": num(c.get("delta")),
                                "gamma": num(c.get("gamma")),
                                "theta": num(c.get("theta")),
                                "vega": num(c.get("vega")),
                                "itm": c.get("inTheMoney"),
                            }
                        )
                    )
    contracts.sort(key=lambda c: (c.get("expiration") or "", c.get("strike") or 0, c.get("put_call") or ""))
    underlying = raw.get("underlying") or {}
    out = {
        "symbol": raw.get("symbol"),
        "status": raw.get("status"),
        "underlying_price": num(raw.get("underlyingPrice")),
        "underlying": compact(
            {
                "last": num(underlying.get("last")),
                "mark": num(underlying.get("mark")),
                "net_change_pct": num(underlying.get("percentChange")),
                "quote_time": ms_to_iso(underlying.get("quoteTime")),
            }
        ),
        "contracts_total": len(contracts),
        "contracts": contracts[:max_contracts],
    }
    if len(contracts) > max_contracts:
        out["truncated_to"] = max_contracts
        out["hint"] = "Narrow with from_date/to_date, strike_count or contract_type to see the rest."
    return compact(out)


def format_expirations(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        compact(
            {
                "expiration": e.get("expirationDate") or e.get("expiration"),
                "dte": e.get("daysToExpiration"),
                "type": e.get("expirationType"),
                "standard": e.get("standard"),
            }
        )
        for e in raw.get("expirationList") or []
    ]


# ------------------------------------------------------------ market hours etc


def format_market_hours(raw: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or now_eastern()
    out = []
    for market, products in (raw or {}).items():
        for product, info in (products or {}).items():
            sessions = {}
            for name, windows in (info.get("sessionHours") or {}).items():
                sessions[snake(name)] = [
                    {"start": w.get("start"), "end": w.get("end")} for w in windows or []
                ]
            entry = {
                "market": market,
                "product": product,
                "product_name": info.get("productName"),
                "date": info.get("date"),
                "is_open": info.get("isOpen"),
                "sessions": sessions,
            }
            entry["session_now"] = (
                session_now(sessions, now) if info.get("date") == now.date().isoformat() else None
            )
            out.append(compact(entry))
    return out


def session_now(sessions: dict[str, list[dict[str, str]]], now: datetime) -> str:
    for name, windows in sessions.items():
        for w in windows:
            try:
                start, end = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if start <= now < end:
                return name
    return "closed"


def format_mover(raw: dict[str, Any]) -> dict[str, Any]:
    return compact({snake(k): (num(v) if isinstance(v, (int, float)) else v) for k, v in raw.items()})


def format_instrument(raw: dict[str, Any]) -> dict[str, Any]:
    fund = raw.get("fundamental") or {}
    keep = (
        "peRatio",
        "eps",
        "divYield",
        "marketCap",
        "high52",
        "low52",
        "beta",
        "sharesOutstanding",
        "returnOnEquity",
        "grossMarginTTM",
        "netProfitMarginTTM",
        "revChangeTTM",
        "epsChangePercentTTM",
        "bookValuePerShare",
        "dividendAmount",
        "dividendDate",
        "dividendPayDate",
    )
    return compact(
        {
            "symbol": raw.get("symbol"),
            "description": raw.get("description"),
            "asset_type": raw.get("assetType"),
            "exchange": raw.get("exchange"),
            "cusip": raw.get("cusip"),
            "fundamental": {
                snake(k): (num(fund[k]) if not isinstance(fund[k], str) else fund[k])
                for k in keep
                if k in fund and fund[k] not in (0, None, "")
            }
            or None,
        }
    )


# ------------------------------------------------------- orders & transactions


def format_order(o: dict[str, Any], *, account_label: str | None) -> dict[str, Any]:
    fills = []
    for activity in o.get("orderActivityCollection") or []:
        for leg in activity.get("executionLegs") or []:
            fills.append(
                compact(
                    {
                        "time": leg.get("time"),
                        "quantity": num(leg.get("quantity")),
                        "price": num(leg.get("price")),
                    }
                )
            )
    legs = [
        compact(
            {
                "instruction": leg.get("instruction"),
                "symbol": (leg.get("instrument") or {}).get("symbol"),
                "asset_type": (leg.get("instrument") or {}).get("assetType"),
                "quantity": num(leg.get("quantity")),
                "position_effect": leg.get("positionEffect"),
            }
        )
        for leg in o.get("orderLegCollection") or []
    ]
    filled_qty = sum(f.get("quantity") or 0 for f in fills)
    avg_fill = (
        (sum((f.get("quantity") or 0) * (f.get("price") or 0) for f in fills) / filled_qty)
        if filled_qty
        else None
    )
    out = {
        "order_id": o.get("orderId"),
        "account": account_label,
        "status": o.get("status"),
        "status_description": o.get("statusDescription"),
        "entered_time": o.get("enteredTime"),
        "close_time": o.get("closeTime"),
        "order_type": o.get("orderType"),
        "strategy": o.get("orderStrategyType"),
        "session": o.get("session"),
        "duration": o.get("duration"),
        "price": num(o.get("price")),
        "stop_price": num(o.get("stopPrice")),
        "quantity": num(o.get("quantity")),
        "filled_quantity": num(o.get("filledQuantity")),
        "remaining_quantity": num(o.get("remainingQuantity")),
        "average_fill_price": num(avg_fill),
        "legs": legs,
        "fills": fills,
    }
    children = o.get("childOrderStrategies") or []
    if children:
        out["child_orders"] = [format_order(c, account_label=account_label) for c in children]
    return compact(out)


def format_transaction(t: dict[str, Any], *, account_label: str | None) -> dict[str, Any]:
    items, fees = [], 0.0
    for item in t.get("transferItems") or []:
        inst = item.get("instrument") or {}
        if item.get("feeType"):
            fees += float(item.get("cost") or 0)
            continue
        if inst.get("assetType") == "CURRENCY":
            continue
        items.append(
            compact(
                {
                    "symbol": inst.get("symbol"),
                    "description": inst.get("description"),
                    "asset_type": inst.get("assetType"),
                    "amount": num(item.get("amount")),
                    "price": num(item.get("price")),
                    "cost": money(item.get("cost")),
                    "position_effect": item.get("positionEffect"),
                }
            )
        )
    return compact(
        {
            "id": t.get("activityId"),
            "account": account_label,
            "time": t.get("time"),
            "trade_date": _date_part(t.get("tradeDate")),
            "type": t.get("type"),
            "status": t.get("status"),
            "description": t.get("description"),
            "net_amount": money(t.get("netAmount")),
            "fees": money(fees) or None,
            "order_id": t.get("orderId"),
            "items": items,
        }
    )
