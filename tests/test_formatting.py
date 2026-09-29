from __future__ import annotations

from datetime import datetime

from conftest import POS_CALL, POS_TSLA_SHORT

from schwab_mcp import formatting as fmt


def test_num_and_compact():
    assert fmt.num(1.0) == 1 and isinstance(fmt.num(1.0), int)
    assert fmt.num("2.123456") == 2.1235
    assert fmt.num(None) is None and fmt.num(True) is None and fmt.num(float("nan")) is None
    assert fmt.compact({"a": None, "b": {}, "c": [], "d": {"e": None, "f": 0}}) == {"d": {"f": 0}}
    assert fmt.snake("netPercentChange") == "net_percent_change"


def test_short_position():
    p = fmt.format_position(POS_TSLA_SHORT, account_value=10000)
    assert p["side"] == "SHORT" and p["quantity"] == -5
    assert p["average_price"] == 250 and p["unrealized_pl"] == -50
    assert p["market_value"] == -1300 and p["price"] == 260
    assert p["weight_pct"] == -13.0


def test_option_position_price_uses_multiplier():
    p = fmt.format_position(POS_CALL, account_value=10000)
    assert p["price"] == 3 and p["cost_basis"] == 500 and p["unrealized_pl_pct"] == 20.0
    assert p["underlying"] == "AAPL" and p["put_call"] == "CALL"


def test_balances_skip_zero_noise():
    b = fmt.format_balances(
        {"liquidationValue": 10, "cashBalance": 0, "shortMarketValue": 0, "buyingPower": 5}
    )
    assert b == {"total_value": 10, "cash_balance": 0, "buying_power": 5}


def test_session_now():
    sessions = {
        "regular_market": [{"start": "2025-09-29T09:30:00-04:00", "end": "2025-09-29T16:00:00-04:00"}]
    }
    at = datetime.fromisoformat("2025-09-29T10:00:00-04:00")
    assert fmt.session_now(sessions, at) == "regular_market"
    assert fmt.session_now(sessions, datetime.fromisoformat("2025-09-29T17:00:00-04:00")) == "closed"


def test_market_hours_marks_current_session():
    raw = {
        "equity": {
            "EQ": {
                "date": "2025-09-29",
                "isOpen": True,
                "productName": "equity",
                "sessionHours": {
                    "regularMarket": [
                        {"start": "2025-09-29T09:30:00-04:00", "end": "2025-09-29T16:00:00-04:00"}
                    ]
                },
            }
        }
    }
    rows = fmt.format_market_hours(raw, now=datetime.fromisoformat("2025-09-29T12:00:00-04:00"))
    assert rows[0]["session_now"] == "regular_market"


def test_price_history_one_day_uses_previous_close():
    raw = {
        "symbol": "X",
        "previousClose": 100.0,
        "candles": [
            {"open": 101, "high": 103, "low": 100, "close": 102, "volume": 10, "datetime": 1759152600000}
        ],
    }
    out = fmt.format_price_history(
        raw, intraday=True, max_candles=10, label={"period": "1D", "interval": "5min"}
    )
    assert out["summary"]["change"] == 2 and out["summary"]["change_pct"] == 2.0
    assert out["summary"]["change_basis"] == "previous_close"
    assert out["candles"][0]["t"] == "2025-09-29T09:30:00-04:00"


def test_empty_price_history():
    out = fmt.format_price_history(
        {"symbol": "X", "candles": [], "empty": True}, intraday=False, max_candles=10, label={"period": "1M"}
    )
    assert out == {"symbol": "X", "period": "1M", "candles_total": 0}


def test_portfolio_summary_does_not_double_count_sweep_position():
    # Schwab may report a money-market sweep both in balances and as a CASH_EQUIVALENT position.
    account = {
        "account": "****0001",
        "balances": {"total_value": 1000, "cash_balance": 0, "money_market_fund": 300},
        "positions": [
            {"symbol": "VTI", "asset_type": "COLLECTIVE_INVESTMENT", "market_value": 700},
            {"symbol": "SWVXX", "asset_type": "CASH_EQUIVALENT", "market_value": 300},
        ],
        "positions_count": 2,
    }
    s = fmt.portfolio_summary([account])
    assert s["total_value"] == 1000 and s["cash_and_sweep"] == 300 and s["invested_value"] == 700
    allocation = {row["asset_type"]: row["pct"] for row in s["allocation_by_asset_type"]}
    assert allocation == {"COLLECTIVE_INVESTMENT": 70.0, "CASH_EQUIVALENT": 30.0}
    assert sum(allocation.values()) == 100
