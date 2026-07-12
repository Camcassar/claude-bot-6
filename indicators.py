"""
Indicators — pure functions over candle lists.

Candle format: {"ts": int_ms, "open": f, "high": f, "low": f, "close": f, "volume": f}
Ordered oldest -> newest.
RSI and ATR use Wilder's smoothing (RMA) to match TradingView's ta.rsi / ta.atr.
"""


def ema(values, period):
    """Exponential moving average. Returns list aligned from index period-1 onward."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma_series(values, period):
    """Simple moving average series (one value per bar from index period-1)."""
    if len(values) < period:
        return []
    out = []
    s = sum(values[:period])
    out.append(s / period)
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out.append(s / period)
    return out


def _rma(values, period):
    """Wilder's smoothing: seed with SMA, then rma = (prev*(n-1) + v)/n."""
    if len(values) < period:
        return []
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append((out[-1] * (period - 1) + v) / period)
    return out


def rsi_series(closes, period):
    """RSI (Wilder). Returns series aligned to the tail of the input."""
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = _rma(gains, period)
    avg_l = _rma(losses, period)
    out = []
    for g, l in zip(avg_g, avg_l):
        if l == 0:
            out.append(100.0 if g > 0 else 50.0)
        else:
            rs = g / l
            out.append(100.0 - 100.0 / (1.0 + rs))
    return out


def atr_series(candles, period):
    """ATR (Wilder) over candle dicts. Returns series aligned to the tail."""
    if len(candles) < period + 1:
        return []
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return _rma(trs, period)
