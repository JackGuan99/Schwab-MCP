"""A small fake of the Schwab API (httpx.MockTransport) plus fixtures wiring it into the MCP server."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

from schwab_mcp.auth import Token, TokenStore
from schwab_mcp.config import Settings
from schwab_mcp.server import build_context, create_server

ACCT_A = "12345678"
ACCT_B = "87654321"
HASH_A = "HASHAAAA"
HASH_B = "HASHBBBB"

ACCOUNT_NUMBERS = [
    {"accountNumber": ACCT_A, "hashValue": HASH_A},
    {"accountNumber": ACCT_B, "hashValue": HASH_B},
]

USER_PREFERENCE = {
    "accounts": [
        {"accountNumber": ACCT_A, "primaryAccount": True, "type": "BROKERAGE", "nickName": "Individual"},
        {"accountNumber": ACCT_B, "primaryAccount": False, "type": "BROKERAGE", "nickName": "Roth IRA"},
    ]
}

POS_AAPL = {
    "shortQuantity": 0,
    "averagePrice": 150.0,
    "currentDayProfitLoss": 25.0,
    "currentDayProfitLossPercentage": 1.49,
    "longQuantity": 10.0,
    "settledLongQuantity": 10.0,
    "instrument": {"assetType": "EQUITY", "cusip": "037833100", "symbol": "AAPL", "description": "APPLE INC"},
    "marketValue": 1700.0,
    "averageLongPrice": 150.0,
    "longOpenProfitLoss": 200.0,
}
POS_SPY = {
    "shortQuantity": 0,
    "averagePrice": 400.0,
    "currentDayProfitLoss": -10.0,
    "currentDayProfitLossPercentage": -0.2,
    "longQuantity": 10.0,
    "instrument": {
        "assetType": "COLLECTIVE_INVESTMENT",
        "symbol": "SPY",
        "description": "SPDR S&P 500",
        "type": "EXCHANGE_TRADED_FUND",
    },
    "marketValue": 5000.0,
    "averageLongPrice": 400.0,
    "longOpenProfitLoss": 1000.0,
}
POS_CALL = {
    "shortQuantity": 0,
    "averagePrice": 2.5,
    "currentDayProfitLoss": 30.0,
    "currentDayProfitLossPercentage": 10.0,
    "longQuantity": 2.0,
    "instrument": {
        "assetType": "OPTION",
        "symbol": "AAPL  260116C00200000",
        "putCall": "CALL",
        "underlyingSymbol": "AAPL",
        "description": "APPLE INC 01/16/2026 $200 Call",
    },
    "marketValue": 600.0,
    "averageLongPrice": 2.5,
    "longOpenProfitLoss": 100.0,
}
POS_TSLA_SHORT = {
    "shortQuantity": 5.0,
    "averagePrice": 250.0,
    "currentDayProfitLoss": -15.0,
    "currentDayProfitLossPercentage": -1.2,
    "longQuantity": 0.0,
    "instrument": {"assetType": "EQUITY", "symbol": "TSLA", "description": "TESLA INC"},
    "marketValue": -1300.0,
    "averageShortPrice": 250.0,
    "shortOpenProfitLoss": -50.0,
}
POS_AAPL_IRA = {
    "shortQuantity": 0,
    "averagePrice": 100.0,
    "currentDayProfitLoss": 12.5,
    "currentDayProfitLossPercentage": 1.49,
    "longQuantity": 5.0,
    "instrument": {"assetType": "EQUITY", "symbol": "AAPL", "description": "APPLE INC"},
    "marketValue": 850.0,
    "averageLongPrice": 100.0,
    "longOpenProfitLoss": 350.0,
}


def account_payload(number: str, positions: bool) -> dict[str, Any]:
    if number == ACCT_A:
        sa = {
            "type": "MARGIN",
            "accountNumber": ACCT_A,
            "roundTrips": 0,
            "isDayTrader": False,
            "positions": [POS_AAPL, POS_SPY, POS_CALL, POS_TSLA_SHORT],
            "currentBalances": {
                "liquidationValue": 10000.0,
                "cashBalance": 4000.0,
                "moneyMarketFund": 0.0,
                "buyingPower": 16000.0,
                "longMarketValue": 7300.0,
                "shortMarketValue": -1300.0,
            },
        }
    else:
        sa = {
            "type": "CASH",
            "accountNumber": ACCT_B,
            "roundTrips": 0,
            "positions": [POS_AAPL_IRA],
            "currentBalances": {
                "liquidationValue": 1000.0,
                "cashBalance": 50.0,
                "moneyMarketFund": 100.0,
                "cashAvailableForTrading": 150.0,
            },
        }
    if not positions:
        sa = {k: v for k, v in sa.items() if k != "positions"}
    return {"securitiesAccount": sa, "aggregatedBalance": {"liquidationValue": 11000.0}}


QUOTES = {
    "AAPL": {
        "assetMainType": "EQUITY",
        "symbol": "AAPL",
        "realtime": True,
        "quote": {
            "52WeekHigh": 199.62,
            "52WeekLow": 164.08,
            "askPrice": 170.1,
            "askSize": 2,
            "bidPrice": 170.0,
            "bidSize": 3,
            "closePrice": 167.5,
            "highPrice": 171.0,
            "lastPrice": 170.05,
            "lowPrice": 166.9,
            "mark": 170.05,
            "netChange": 2.55,
            "netPercentChange": 1.5224,
            "openPrice": 168.0,
            "quoteTime": 1759176000000,
            "totalVolume": 50_000_000,
            "tradeTime": 1759176000000,
            "securityStatus": "Normal",
        },
        "reference": {"description": "Apple Inc", "exchangeName": "NASDAQ"},
        "fundamental": {
            "peRatio": 28.1,
            "eps": 6.05,
            "divYield": 0.55,
            "divAmount": 0.96,
            "divExDate": "2025-08-11 00:00:00.0",
            "avg10DaysVolume": 51000000,
        },
        "regular": {
            "regularMarketLastPrice": 170.05,
            "regularMarketNetChange": 2.55,
            "regularMarketPercentChange": 1.5224,
            "regularMarketTradeTime": 1759176000000,
        },
    },
    "SPY": {
        "assetMainType": "EQUITY",
        "symbol": "SPY",
        "realtime": True,
        "quote": {
            "lastPrice": 500.0,
            "mark": 500.0,
            "netChange": -1.0,
            "netPercentChange": -0.2,
            "quoteTime": 1759176000000,
        },
        "reference": {"description": "SPDR S&P 500 ETF"},
    },
    "TSLA": {
        "assetMainType": "EQUITY",
        "symbol": "TSLA",
        "realtime": True,
        "quote": {
            "lastPrice": 260.0,
            "mark": 260.0,
            "netChange": 3.0,
            "netPercentChange": 1.17,
            "quoteTime": 1759176000000,
        },
    },
    "AAPL  260116C00200000": {
        "assetMainType": "OPTION",
        "symbol": "AAPL  260116C00200000",
        "realtime": True,
        "quote": {
            "lastPrice": 3.0,
            "mark": 3.0,
            "delta": 0.35,
            "volatility": 28.5,
            "openInterest": 1200,
            "underlyingPrice": 170.05,
            "quoteTime": 1759176000000,
        },
        "reference": {
            "contractType": "C",
            "underlying": "AAPL",
            "strikePrice": 200.0,
            "expirationDate": "2026-01-16T21:00:00.000+00:00",
            "multiplier": 100,
        },
    },
}

TRANSACTIONS = {
    HASH_A: [
        {
            "activityId": 1,
            "time": "2025-09-20T14:30:00+0000",
            "accountNumber": ACCT_A,
            "type": "TRADE",
            "status": "VALID",
            "netAmount": -1500.65,
            "orderId": 555,
            "tradeDate": "2025-09-20T00:00:00+0000",
            "description": f"Bought in account {ACCT_A}",
            "transferItems": [
                {
                    "instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"},
                    "amount": 0,
                    "cost": -0.65,
                    "feeType": "COMMISSION",
                },
                {
                    "instrument": {"assetType": "EQUITY", "symbol": "AAPL", "description": "APPLE INC"},
                    "amount": 10,
                    "cost": -1500.0,
                    "price": 150.0,
                    "positionEffect": "OPENING",
                },
            ],
        },
    ],
    HASH_B: [
        {
            "activityId": 2,
            "time": "2025-09-25T12:00:00+0000",
            "accountNumber": int(ACCT_B),
            "type": "DIVIDEND_OR_INTEREST",
            "status": "VALID",
            "netAmount": 1.2,
            "description": "QUALIFIED DIVIDEND",
            "transferItems": [{"instrument": {"assetType": "EQUITY", "symbol": "AAPL"}, "amount": 1.2}],
        },
    ],
}

ORDERS = [
    {
        "orderId": 555,
        "accountNumber": int(ACCT_A),
        "status": "FILLED",
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "price": 150.0,
        "quantity": 10,
        "filledQuantity": 10,
        "remainingQuantity": 0,
        "orderStrategyType": "SINGLE",
        "enteredTime": "2025-09-20T14:29:00+0000",
        "orderLegCollection": [
            {
                "instruction": "BUY",
                "quantity": 10,
                "positionEffect": "OPENING",
                "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
            }
        ],
        "orderActivityCollection": [
            {
                "executionLegs": [
                    {"quantity": 4, "price": 149.9, "time": "t1"},
                    {"quantity": 6, "price": 150.0, "time": "t2"},
                ]
            }
        ],
    },
]

PRICE_HISTORY = {
    "symbol": "AAPL",
    "empty": False,
    "previousClose": 167.5,
    "candles": [
        {
            "open": 160 + i,
            "high": 162 + i,
            "low": 159 + i,
            "close": 161 + i,
            "volume": 1000 * (i + 1),
            "datetime": 1756699200000 + i * 86_400_000,
        }
        for i in range(30)
    ],
}

OPTION_CHAIN = {
    "symbol": "AAPL",
    "status": "SUCCESS",
    "underlyingPrice": 170.05,
    "underlying": {"last": 170.05, "mark": 170.05, "percentChange": 1.52, "quoteTime": 1759176000000},
    "callExpDateMap": {
        "2025-10-17:18": {
            "170.0": [
                {
                    "putCall": "CALL",
                    "symbol": "AAPL  251017C00170000",
                    "bid": 4.9,
                    "ask": 5.1,
                    "mark": 5.0,
                    "last": 5.0,
                    "totalVolume": 900,
                    "openInterest": 5000,
                    "volatility": 25.123,
                    "delta": 0.52,
                    "gamma": 0.03,
                    "theta": -0.1,
                    "vega": 0.2,
                    "strikePrice": 170.0,
                    "expirationDate": "2025-10-17T20:00:00.000+00:00",
                    "daysToExpiration": 18,
                    "inTheMoney": True,
                }
            ],
        },
    },
    "putExpDateMap": {
        "2025-10-17:18": {
            "170.0": [
                {
                    "putCall": "PUT",
                    "symbol": "AAPL  251017P00170000",
                    "bid": 4.7,
                    "ask": 4.9,
                    "mark": 4.8,
                    "strikePrice": 170.0,
                    "expirationDate": "2025-10-17T20:00:00.000+00:00",
                    "daysToExpiration": 18,
                    "inTheMoney": False,
                }
            ],
        },
    },
}


class FakeSchwab:
    """Routes requests like the real API and records them for assertions."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_next: list[httpx.Response] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            return self.fail_next.pop(0)
        path = request.url.path
        params = request.url.params
        if path == "/v1/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "refresh-1",
                    "expires_in": 1800,
                    "token_type": "Bearer",
                    "scope": "api",
                },
            )
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"message": "no token"})
        positions = params.get("fields") == "positions"
        routes: dict[str, Any] = {
            "/trader/v1/accounts/accountNumbers": ACCOUNT_NUMBERS,
            "/trader/v1/userPreference": USER_PREFERENCE,
            "/trader/v1/accounts": [account_payload(ACCT_A, positions), account_payload(ACCT_B, positions)],
            f"/trader/v1/accounts/{HASH_A}": account_payload(ACCT_A, positions),
            f"/trader/v1/accounts/{HASH_B}": account_payload(ACCT_B, positions),
            "/trader/v1/orders": ORDERS,
            f"/trader/v1/accounts/{HASH_A}/orders": ORDERS,
            f"/trader/v1/accounts/{HASH_A}/transactions": TRANSACTIONS[HASH_A],
            f"/trader/v1/accounts/{HASH_B}/transactions": TRANSACTIONS[HASH_B],
            "/marketdata/v1/pricehistory": PRICE_HISTORY,
            "/marketdata/v1/chains": OPTION_CHAIN,
            "/marketdata/v1/expirationchain": {
                "expirationList": [
                    {
                        "expirationDate": "2025-10-17",
                        "daysToExpiration": 18,
                        "expirationType": "S",
                        "standard": True,
                    }
                ]
            },
            "/marketdata/v1/movers/$SPX": {
                "screeners": [
                    {
                        "symbol": "NVDA",
                        "description": "NVIDIA",
                        "lastPrice": 180.0,
                        "netChange": 9.0,
                        "netPercentChange": 0.0526,
                        "volume": 1000,
                        "totalVolume": 200000000,
                    }
                ]
            },
            "/marketdata/v1/instruments": {
                "instruments": [
                    {
                        "symbol": "NVDA",
                        "description": "NVIDIA CORP",
                        "assetType": "EQUITY",
                        "exchange": "NASDAQ",
                        "cusip": "67066G104",
                        "fundamental": {"peRatio": 50.0, "marketCap": 4.4e12, "beta": 1.7},
                    }
                ]
            },
        }
        if path == "/marketdata/v1/quotes":
            symbols = params.get("symbols", "").split(",")
            body: dict[str, Any] = {s: QUOTES[s] for s in symbols if s in QUOTES}
            invalid = [s for s in symbols if s not in QUOTES]
            if invalid:
                body["errors"] = {"invalidSymbols": invalid}
            return httpx.Response(200, json=body)
        if path == "/marketdata/v1/markets":
            today = params.get("date") or "2025-09-29"
            return httpx.Response(
                200,
                json={
                    "equity": {
                        "EQ": {
                            "date": today,
                            "marketType": "EQUITY",
                            "product": "EQ",
                            "productName": "equity",
                            "isOpen": True,
                            "sessionHours": {
                                "preMarket": [
                                    {"start": f"{today}T07:00:00-04:00", "end": f"{today}T09:30:00-04:00"}
                                ],
                                "regularMarket": [
                                    {"start": f"{today}T09:30:00-04:00", "end": f"{today}T16:00:00-04:00"}
                                ],
                                "postMarket": [
                                    {"start": f"{today}T16:00:00-04:00", "end": f"{today}T20:00:00-04:00"}
                                ],
                            },
                        }
                    }
                },
            )
        if path in routes:
            return httpx.Response(200, json=routes[path])
        return httpx.Response(404, json={"errors": [{"status": 404, "title": "Not Found", "detail": path}]})

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def write_token(path: Path, *, expires_in: float = 1800, issued_ago: float = 3600) -> Token:
    now = time.time()
    token = Token(
        access_token="access-0",
        refresh_token="refresh-0",
        expires_at=now + expires_in,
        refresh_token_issued_at=now - issued_ago,
    )
    TokenStore(path).save(token)
    return token


@pytest.fixture
def fake() -> FakeSchwab:
    return FakeSchwab()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    path = tmp_path / "token.json"
    write_token(path)
    return Settings(client_id="app-key", client_secret="app-secret", token_path=path)


@pytest.fixture
def make_client(fake: FakeSchwab, settings: Settings):
    def _make(**overrides: Any) -> Client:
        s = Settings(**{**settings.__dict__, **overrides})

        @asynccontextmanager
        async def factory():
            async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
                yield build_context(s, http)

        return Client(create_server(s, context_factory=factory))

    return _make


def payload(result) -> Any:
    assert not result.is_error, result.content
    return (
        result.structured_content
        if result.structured_content is not None
        else json.loads(result.content[0].text)
    )
