"""End-to-end: MCP client -> server tools -> fake Schwab API."""

from __future__ import annotations

import json

from conftest import ACCT_A, ACCT_B, HASH_A, payload


async def test_tools_are_read_only(make_client):
    async with make_client() as client:
        tools = (await client.list_tools()).tools
    names = {t.name for t in tools}
    assert {"get_positions", "get_quotes", "get_portfolio_summary", "list_accounts"} <= names
    assert not any(word in n for n in names for word in ("place", "cancel", "replace", "trade"))
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)


async def test_list_accounts_masks_numbers(make_client):
    async with make_client() as client:
        data = payload(await client.call_tool("list_accounts", {}))
    assert data["total_value"] == 11000
    first, second = data["accounts"]
    assert first["account"] == "****5678" and first["nickname"] == "Individual" and first["primary"] is True
    assert first["balances"]["buying_power"] == 16000
    assert first["type"] == "MARGIN"
    assert second["account"] == "****4321" and second["type"] == "CASH"
    assert "positions" not in first


async def test_positions_all_accounts_with_quotes(make_client, fake):
    async with make_client() as client:
        data = payload(await client.call_tool("get_positions", {"include_quotes": True}))
    acct_a = data["accounts"][0]
    assert acct_a["positions_count"] == 4
    assert [p["symbol"] for p in acct_a["positions"]][:2] == ["SPY", "AAPL"]  # sorted by |market value|
    aapl = next(p for p in acct_a["positions"] if p["symbol"] == "AAPL")
    assert aapl["cost_basis"] == 1500 and aapl["unrealized_pl_pct"] == 13.33 and aapl["weight_pct"] == 17.0
    assert aapl["live"]["last"] == 170.05
    quote_calls = [r for r in fake.requests if r.url.path == "/marketdata/v1/quotes"]
    assert len(quote_calls) == 1 and quote_calls[0].url.params["fields"] == "quote"


async def test_positions_resolve_account_by_digits_and_nickname(make_client, fake):
    async with make_client() as client:
        by_digits = payload(await client.call_tool("get_positions", {"account": "4321"}))
        by_nick = payload(await client.call_tool("get_positions", {"account": "roth ira"}))
        by_label = payload(await client.call_tool("get_positions", {"account": "****4321"}))
        missing = await client.call_tool("get_positions", {"account": "9999"})
    assert by_digits["accounts"] == by_nick["accounts"] == by_label["accounts"]
    assert by_digits["accounts"][0]["account"] == "****4321"
    assert missing.is_error and "Available accounts" in missing.content[0].text
    assert "/trader/v1/accounts/HASHBBBB" in fake.paths()


async def test_positions_symbol_filter_includes_options_on_underlying(make_client):
    async with make_client() as client:
        data = payload(await client.call_tool("get_positions", {"account": "Individual", "symbol": "aapl"}))
    symbols = [p["symbol"] for p in data["accounts"][0]["positions"]]
    assert sorted(symbols) == ["AAPL", "AAPL  260116C00200000"]


async def test_portfolio_summary(make_client):
    async with make_client() as client:
        data = payload(await client.call_tool("get_portfolio_summary", {"top_n": 3}))
    assert data["total_value"] == 11000
    assert data["cash_and_sweep"] == 4150
    assert data["positions_count"] == 5 and data["distinct_holdings"] == 4
    top = data["top_holdings"]
    assert top[0]["symbol"] == "SPY"
    aapl = next(h for h in top if h["symbol"] == "AAPL")
    assert aapl["quantity"] == 15 and aapl["market_value"] == 2550
    assert aapl["accounts"] == ["****4321", "****5678"]
    types = {row["asset_type"]: row for row in data["allocation_by_asset_type"]}
    assert types["CASH_AND_SWEEP"]["market_value"] == 4150


async def test_no_raw_account_number_leaks(make_client):
    calls = [
        ("list_accounts", {}),
        ("get_positions", {"include_quotes": True}),
        ("get_portfolio_summary", {}),
        ("get_orders", {}),
        ("get_transactions", {}),
    ]
    async with make_client() as client:
        for name, args in calls:
            result = await client.call_tool(name, args)
            text = json.dumps(payload(result)) + "".join(c.text for c in result.content)
            assert ACCT_A not in text and ACCT_B not in text, name
            assert HASH_A not in text, name


async def test_masking_can_be_disabled(make_client):
    async with make_client(mask_accounts=False) as client:
        data = payload(await client.call_tool("list_accounts", {}))
    assert data["accounts"][0]["account"] == ACCT_A


async def test_quotes_accepts_comma_string_and_reports_invalid(make_client, fake):
    async with make_client() as client:
        data = payload(await client.call_tool("get_quotes", {"symbols": "aapl, spy ,NOPE"}))
    assert set(data["quotes"]) == {"AAPL", "SPY"}
    assert data["invalid_symbols"] == ["NOPE"]
    aapl = data["quotes"]["AAPL"]
    assert aapl["last"] == 170.05 and aapl["high_52w"] == 199.62 and aapl["previous_close"] == 167.5
    assert aapl["fundamental"]["ex_dividend_date"] == "2025-08-11"
    assert aapl["quote_time"].endswith("-04:00")
    sent = [r for r in fake.requests if r.url.path == "/marketdata/v1/quotes"][0]
    assert sent.url.params["symbols"] == "AAPL,SPY,NOPE"
    assert "fields" not in sent.url.params


async def test_option_quote_has_greeks(make_client):
    async with make_client() as client:
        data = payload(await client.call_tool("get_quotes", {"symbols": ["AAPL  260116C00200000"]}))
    q = data["quotes"]["AAPL  260116C00200000"]
    assert q["delta"] == 0.35 and q["implied_volatility"] == 28.5
    assert q["option"] == {
        "underlying": "AAPL",
        "put_call": "CALL",
        "strike": 200,
        "expiration": "2026-01-16",
        "multiplier": 100,
    }


async def test_price_history_presets_and_validation(make_client, fake):
    async with make_client() as client:
        data = payload(
            await client.call_tool("get_price_history", {"symbol": "aapl", "period": "3M", "max_candles": 5})
        )
        bad = await client.call_tool(
            "get_price_history", {"symbol": "AAPL", "period": "1Y", "interval": "5min"}
        )
    params = [r for r in fake.requests if r.url.path == "/marketdata/v1/pricehistory"][0].url.params
    assert params["periodType"] == "month" and params["period"] == "3" and params["frequencyType"] == "daily"
    assert data["candles_total"] == 30 and len(data["candles"]) == 5 and data["truncated_to_last"] == 5
    assert (
        data["summary"]["open"] == 160 and data["summary"]["close"] == 190 and data["summary"]["high"] == 191
    )
    assert data["summary"]["change_pct"] == 18.75
    assert data["candles"][0]["t"] == "2025-09-26"
    assert bad.is_error and "not available" in bad.content[0].text


async def test_price_history_date_range(make_client, fake):
    async with make_client() as client:
        payload(
            await client.call_tool(
                "get_price_history",
                {"symbol": "AAPL", "start_date": "2025-09-01", "end_date": "2025-09-05", "interval": "daily"},
            )
        )
    params = [r for r in fake.requests if r.url.path == "/marketdata/v1/pricehistory"][0].url.params
    assert params["periodType"] == "year" and "period" not in params
    assert params["startDate"] == "1756699200000"  # 2025-09-01 00:00 US/Eastern
    assert params["endDate"] == "1757131200000"  # 2025-09-06 00:00 US/Eastern (end date inclusive)


async def test_option_chain_and_expirations(make_client):
    async with make_client() as client:
        chain = payload(await client.call_tool("get_option_chain", {"symbol": "aapl", "max_contracts": 1}))
        exps = payload(await client.call_tool("get_option_expirations", {"symbol": "AAPL"}))
    assert chain["contracts_total"] == 2 and chain["truncated_to"] == 1
    c = chain["contracts"][0]
    assert (
        c["put_call"] == "CALL" and c["expiration"] == "2025-10-17" and c["iv"] == 25.12 and c["itm"] is True
    )
    assert exps["expirations"][0]["dte"] == 18


async def test_orders_and_transactions(make_client, fake):
    async with make_client() as client:
        orders = payload(await client.call_tool("get_orders", {"days": 3}))
        txs = payload(
            await client.call_tool("get_transactions", {"types": ["TRADE", "DIVIDEND_OR_INTEREST"]})
        )
        one = payload(await client.call_tool("get_transactions", {"account": "5678", "symbol": "aapl"}))
    order = orders["orders"][0]
    assert order["account"] == "****5678" and order["average_fill_price"] == 149.96
    assert [t["id"] for t in txs["transactions"]] == [2, 1]  # newest first, both accounts
    trade = txs["transactions"][1]
    assert trade["fees"] == -0.65 and trade["items"][0]["symbol"] == "AAPL"
    assert "****5678" in trade["description"]
    assert one["count"] == 1
    tx_req = [r for r in fake.requests if r.url.path.endswith("/transactions")]
    assert tx_req[0].url.params["types"] == "TRADE,DIVIDEND_OR_INTEREST"
    assert tx_req[0].url.params["startDate"].endswith(".000Z")
    assert tx_req[-1].url.params["symbol"] == "AAPL"
    order_req = [r for r in fake.requests if r.url.path == "/trader/v1/orders"][0]
    assert order_req.url.params["fromEnteredTime"].endswith(".000Z")


async def test_market_hours_movers_instruments_time(make_client):
    async with make_client() as client:
        hours = payload(await client.call_tool("get_market_hours", {"date": "2025-09-29"}))
        movers = payload(await client.call_tool("get_movers", {}))
        inst = payload(
            await client.call_tool("search_instruments", {"query": "nvda", "search_type": "fundamental"})
        )
        now = payload(await client.call_tool("get_current_time", {}))
    eq = hours["markets"][0]
    assert eq["is_open"] is True and set(eq["sessions"]) == {"pre_market", "regular_market", "post_market"}
    assert movers["movers"][0]["symbol"] == "NVDA" and movers["movers"][0]["net_percent_change"] == 0.0526
    assert inst["instruments"][0]["fundamental"]["market_cap"] == 4.4e12
    assert "eastern" in now and "utc" in now


async def test_expired_login_gives_actionable_error(make_client, settings):
    from conftest import write_token

    write_token(settings.token_path, expires_in=-10, issued_ago=8 * 24 * 3600)
    async with make_client() as client:
        result = await client.call_tool("get_quotes", {"symbols": ["AAPL"]})
        status = payload(await client.call_tool("get_auth_status", {}))
    assert result.is_error and "schwab-mcp auth" in result.content[0].text
    assert status["authenticated"] is False and "expired" in status["warning"]


async def test_technical_indicators(make_client, fake):
    async with make_client() as client:
        data = payload(await client.call_tool("get_technical_indicators", {"symbols": "trend, empty, bad"}))
        weekly = payload(
            await client.call_tool("get_technical_indicators", {"symbols": ["TREND"], "interval": "weekly"})
        )
        all_bad = await client.call_tool("get_technical_indicators", {"symbols": ["BAD"]})
        too_many = await client.call_tool(
            "get_technical_indicators", {"symbols": [f"S{i}" for i in range(26)]}
        )
    trend = data["indicators"]["TREND"]
    assert trend["bars"] == 300 and trend["trend"]["sma50_above_sma200"] is True
    assert set(data["indicators"]) == {"TREND"}
    assert data["errors"]["EMPTY"].startswith("Not enough price history")
    assert "Invalid symbol BAD" in data["errors"]["BAD"]
    assert weekly["indicators"]["TREND"]["interval"] == "weekly"
    assert all_bad.is_error and "Invalid symbol BAD" in all_bad.content[0].text
    assert too_many.is_error and "At most 25" in too_many.content[0].text
    history = [r.url.params for r in fake.requests if r.url.path == "/marketdata/v1/pricehistory"]
    daily = next(p for p in history if p["symbol"] == "TREND" and p["frequencyType"] == "daily")
    assert (daily["periodType"], daily["period"], daily["frequency"]) == ("year", "2", "1")
    assert any(p["frequencyType"] == "weekly" and p["period"] == "10" for p in history)
