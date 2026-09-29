"""Technical indicators computed from price-history candles.

LLMs are unreliable at arithmetic over hundreds of candles, so the server computes the
indicators an analyst usually asks for and returns only the latest values plus a few
facts (crossovers, distance from moving averages / 52-week range).

Conventions match common charting platforms: EMA seeded with the SMA of its first
window; RSI and ATR use Wilder's smoothing; Bollinger bands use the population
standard deviation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from .formatting import compact, ms_to_iso, num, pct

Series = list[float | None]

# Lookbacks expressed in bars for each supported candle size.
BARS = {
    "daily": {
        "returns": {"1w": 5, "1m": 21, "3m": 63, "6m": 126, "1y": 252},
        "year": 252,
        "vol_short": ("20d", 20),
    },
    "weekly": {
        "returns": {"1m": 4, "3m": 13, "6m": 26, "1y": 52, "3y": 156},
        "year": 52,
        "vol_short": ("13w", 13),
    },
}
SMA_WINDOWS = (20, 50, 200)


# ---------------------------------------------------------------- primitives


def sma(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    window = sum(values[:n])
    out[n - 1] = window / n
    for i in range(n, len(values)):
        window += values[i] - values[i - n]
        out[i] = window / n
    return out


def ema(values: Sequence[float | None], n: int) -> Series:
    """EMA seeded with the SMA of the first ``n`` defined values (leading Nones are skipped)."""
    out: Series = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or n <= 0 or len(values) - start < n:
        return out
    seed = values[start : start + n]
    prev = sum(seed) / n  # type: ignore[arg-type]
    out[start + n - 1] = prev
    alpha = 2 / (n + 1)
    for i in range(start + n, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev  # type: ignore[operator]
        out[i] = prev
    return out


def _wilder(values: Sequence[float], n: int, offset: int) -> Series:
    """Wilder smoothing of ``values`` whose element 0 corresponds to output index ``offset``."""
    out: Series = [None] * (len(values) + offset)
    if len(values) < n:
        return out
    avg = sum(values[:n]) / n
    out[offset + n - 1] = avg
    for i in range(n, len(values)):
        avg = (avg * (n - 1) + values[i]) / n
        out[offset + i] = avg
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    if len(closes) <= n:
        return [None] * len(closes)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = _wilder([max(d, 0.0) for d in deltas], n, 1)
    losses = _wilder([max(-d, 0.0) for d in deltas], n, 1)
    out: Series = []
    for g, loss in zip(gains, losses, strict=True):
        if g is None or loss is None:
            out.append(None)
        elif loss == 0:
            out.append(50.0 if g == 0 else 100.0)
        else:
            out.append(100 - 100 / (1 + g / loss))
    return out


def _diff(a: Series, b: Series) -> Series:
    return [x - y if x is not None and y is not None else None for x, y in zip(a, b, strict=True)]


def macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Series, Series, Series]:
    line = _diff(ema(closes, fast), ema(closes, slow))
    sig = ema(line, signal)
    return line, sig, _diff(line, sig)


def bollinger(closes: Sequence[float], n: int = 20, k: float = 2.0) -> tuple[Series, Series, Series]:
    mid = sma(closes, n)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        m = mid[i]
        if m is None:
            continue
        sd = math.sqrt(sum((c - m) ** 2 for c in closes[i - n + 1 : i + 1]) / n)
        upper[i], lower[i] = m + k * sd, m - k * sd
    return upper, mid, lower


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14) -> Series:
    true_ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    return _wilder(true_ranges, n, 1)


def realized_volatility(closes: Sequence[float], n: int, periods_per_year: int) -> float | None:
    """Annualized standard deviation (sample) of the last ``n`` log returns, in percent."""
    if len(closes) < n + 1:
        return None
    window = closes[-(n + 1) :]
    if any(c <= 0 for c in window):
        return None
    rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year) * 100


def last_cross(a: Series, b: Series) -> tuple[int, str] | None:
    """Index and direction ("up": a crossed above b, "down": below) of the most recent crossover."""
    for i in range(len(a) - 1, 0, -1):
        a0, b0, a1, b1 = a[i - 1], b[i - 1], a[i], b[i]
        if None in (a0, b0, a1, b1):
            return None
        if a0 <= b0 and a1 > b1:  # type: ignore[operator]
            return i, "up"
        if a0 >= b0 and a1 < b1:  # type: ignore[operator]
            return i, "down"
    return None


# ------------------------------------------------------------------- summary


def _clean(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keep = []
    for c in candles:
        try:
            keep.append(
                {
                    "t": c.get("datetime"),
                    "h": float(c["high"]),
                    "l": float(c["low"]),
                    "c": float(c["close"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return keep


def technical_summary(raw: dict[str, Any], *, interval: str = "daily") -> dict[str, Any]:
    """Latest indicator values for one symbol's price history (``/pricehistory`` payload)."""
    cfg = BARS[interval]
    bars = _clean(raw.get("candles") or [])
    if len(bars) < 2:
        return {"error": "Not enough price history to compute indicators."}
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    last = closes[-1]
    n = len(bars)

    def date_at(i: int) -> str | None:
        return ms_to_iso(bars[i]["t"], date_only=True)

    smas = {w: sma(closes, w) for w in SMA_WINDOWS}
    macd_line, macd_signal, macd_hist = macd(closes)
    upper, mid, lower = bollinger(closes)
    rsi_now = rsi(closes)[-1]
    atr_now = atr(highs, lows, closes)[-1]

    returns = {k: pct(last - closes[-1 - b], closes[-1 - b]) for k, b in cfg["returns"].items() if n > b}
    out: dict[str, Any] = {
        "interval": interval,
        "bars": n,
        "first_bar": date_at(0),
        "last_bar": date_at(-1),
        "close": num(last),
        "change_pct": returns,
        "sma": {str(w): num(s[-1]) for w, s in smas.items()},
        "close_vs_sma_pct": {str(w): pct(last - s[-1], s[-1]) if s[-1] else None for w, s in smas.items()},
        "rsi_14": num(rsi_now, 2),
    }
    if rsi_now is not None:
        out["rsi_zone"] = "overbought" if rsi_now >= 70 else "oversold" if rsi_now <= 30 else "neutral"

    if macd_hist[-1] is not None:
        out["macd"] = {
            "macd": num(macd_line[-1]),
            "signal": num(macd_signal[-1]),
            "histogram": num(macd_hist[-1]),
        }
        cross = last_cross(macd_line, macd_signal)
        if cross:
            i, direction = cross
            out["macd"]["last_cross"] = {
                "type": "bullish" if direction == "up" else "bearish",
                "date": date_at(i),
                "bars_ago": n - 1 - i,
            }

    if smas[50][-1] is not None and smas[200][-1] is not None:
        trend: dict[str, Any] = {
            "sma50_above_sma200": smas[50][-1] > smas[200][-1],
            "sma50_vs_sma200_pct": pct(smas[50][-1] - smas[200][-1], smas[200][-1]),
        }
        cross = last_cross(smas[50], smas[200])
        if cross:
            i, direction = cross
            trend["last_cross"] = {
                "type": "golden_cross" if direction == "up" else "death_cross",
                "date": date_at(i),
                "bars_ago": n - 1 - i,
            }
        out["trend"] = trend

    if upper[-1] is not None:
        width = upper[-1] - lower[-1]  # type: ignore[operator]
        out["bollinger_20_2"] = {
            "upper": num(upper[-1]),
            "middle": num(mid[-1]),
            "lower": num(lower[-1]),
            "percent_b": num((last - lower[-1]) / width, 3) if width else None,  # type: ignore[operator]
            "bandwidth_pct": pct(width, mid[-1]),
        }

    if atr_now is not None:
        out["atr_14"] = num(atr_now)
        out["atr_pct"] = pct(atr_now, last)

    year = cfg["year"]
    short_label, short_n = cfg["vol_short"]
    out["volatility_annualized_pct"] = {
        short_label: num(realized_volatility(closes, short_n, year), 2),
        "1y": num(realized_volatility(closes, min(year, n - 1), year), 2) if n > year // 2 else None,
    }

    window = bars[-year:]
    high = max(b["h"] for b in window)
    low = min(b["l"] for b in window)
    out["range_52w"] = {
        "high": num(high),
        "low": num(low),
        "pct_below_high": pct(high - last, high),
        "pct_above_low": pct(last - low, low),
        "partial": True if n < year else None,
    }
    return compact(out)
