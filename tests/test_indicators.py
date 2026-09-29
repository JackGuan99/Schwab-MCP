from __future__ import annotations

import math

import pytest
from conftest import trend_history

from schwab_mcp import indicators as ind

# Wilder RSI worked example published by StockCharts ("Relative Strength Index (RSI)", ChartSchool).
STOCKCHARTS_CLOSES = [
    44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826, 45.8931,
    46.0328, 45.614, 46.282, 46.282, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439, 46.2122, 46.2521,
    45.7137, 46.4515, 45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672, 43.4205, 42.6628, 43.1314,
]  # fmt: skip
STOCKCHARTS_RSI = [
    70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38,
    54.71, 50.42, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77,
]  # fmt: skip


def test_sma_and_ema():
    assert ind.sma([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]
    assert ind.sma([1, 2], 3) == [None, None]
    assert ind.ema([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]  # seed 2, alpha 0.5
    assert ind.ema([None, None, 3, 3, 3, 3], 2) == [None, None, None, 3, 3, 3]


def test_rsi_matches_stockcharts_example():
    values = [round(v, 2) for v in ind.rsi(STOCKCHARTS_CLOSES) if v is not None]
    assert values == STOCKCHARTS_RSI


def test_rsi_edge_cases():
    assert ind.rsi(list(range(1, 30)))[-1] == 100
    assert ind.rsi(list(range(30, 1, -1)))[-1] == 0
    assert ind.rsi([5.0] * 20)[-1] == 50
    assert ind.rsi([1.0] * 14) == [None] * 14


def test_macd_is_ema_difference():
    closes = [c["close"] for c in trend_history(120)["candles"]]
    line, signal, hist = ind.macd(closes)
    fast, slow = ind.ema(closes, 12), ind.ema(closes, 26)
    assert line[24] is None and line[25] == pytest.approx(fast[25] - slow[25])
    assert signal[25 + 7] is None and signal[25 + 8] is not None
    assert hist[-1] == pytest.approx(line[-1] - signal[-1])


def test_bollinger_and_atr():
    upper, mid, lower = ind.bollinger([10.0] * 25)
    assert upper[-1] == mid[-1] == lower[-1] == 10
    upper, mid, lower = ind.bollinger([1, 3] * 10)  # population sd of alternating 1/3 is exactly 1
    assert (upper[-1], mid[-1], lower[-1]) == (4, 2, 0)
    closes = [100.0 + i for i in range(20)]
    highs, lows = [c + 1 for c in closes], [c - 1 for c in closes]
    # true range = max(2, |h - prev close| = 2, |l - prev close| = 0) = 2 on every bar
    assert ind.atr(highs, lows, closes)[-1] == pytest.approx(2)


def test_realized_volatility():
    assert ind.realized_volatility([100.0] * 30, 20, 252) == 0
    alternating = [100.0, 101.0] * 15
    vol = ind.realized_volatility(alternating, 20, 252)
    r = math.log(1.01)
    expected = math.sqrt(20 * r * r / 19) * math.sqrt(252) * 100  # mean of +/-r is 0
    assert vol == pytest.approx(expected)
    assert ind.realized_volatility([100.0] * 5, 20, 252) is None


def test_last_cross():
    a = [1, 2, 3, 4, 3, 2]
    b = [2, 2, 2, 2, 2.5, 2.5]
    assert ind.last_cross(a, b) == (5, "down")  # the up-cross at index 2 is older
    assert ind.last_cross([1, 3], [2, 2]) == (1, "up")
    assert ind.last_cross([3, 3], [2, 2]) is None
    assert ind.last_cross([None, 3], [2, 2]) is None


def test_technical_summary_on_uptrend():
    out = ind.technical_summary(trend_history(300), interval="daily")
    assert out["bars"] == 300 and out["first_bar"] == "2024-12-03"
    assert set(out["sma"]) == {"20", "50", "200"}
    assert out["sma"]["200"] < out["sma"]["50"]
    assert out["close_vs_sma_pct"]["200"] > 0
    assert out["trend"]["sma50_above_sma200"] is True
    assert 0 < out["rsi_14"] < 100 and out["rsi_zone"] in {"overbought", "oversold", "neutral"}
    assert {"macd", "signal", "histogram"} <= set(out["macd"])
    assert out["macd"]["last_cross"]["type"] in {"bullish", "bearish"}
    assert out["atr_14"] > 0 and out["bollinger_20_2"]["upper"] > out["bollinger_20_2"]["lower"]
    assert set(out["change_pct"]) == {"1w", "1m", "3m", "6m", "1y"}
    assert out["range_52w"]["high"] >= out["close"] >= out["range_52w"]["low"]
    assert "partial" not in out["range_52w"]
    assert set(out["volatility_annualized_pct"]) == {"20d", "1y"}


def test_technical_summary_short_history():
    out = ind.technical_summary(trend_history(30), interval="daily")
    assert "200" not in out["sma"] and "50" not in out["sma"] and "20" in out["sma"]
    assert "trend" not in out and out["range_52w"]["partial"] is True
    assert ind.technical_summary({"candles": []}) == {
        "error": "Not enough price history to compute indicators."
    }
