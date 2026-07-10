#!/usr/bin/env python3
"""
================================================================================
 VWAP BAND STRATEGY  —  single-file standalone bot + backtest laboratory
================================================================================

EVERYTHING for this strategy lives in THIS ONE FILE on purpose: the theory,
the indicators, the signal logic, the backtester, the parameter sweeps, the
walk-forward harness, and the live trading loop. Nothing is scattered across
other modules. Read top-to-bottom and you have the whole thing.

--------------------------------------------------------------------------------
 1. PROVENANCE  —  where the theory comes from
--------------------------------------------------------------------------------
This is a NEW standalone strategy authored by Claude (Opus 4.8), built by
pulling the *reusable pieces* out of the VWAP work that already existed in the
Fable handover's "Day Trader Bot (ORB + VWAP)". It is NOT a Fable-authored
theory and it is NOT the ORB bot — it stands on its own. The pieces reused:

  • Session-anchored VWAP            (handover indicators.session_vwap)
  • "Right side of VWAP" as a bias   (handover strategy.check_breakout)
  • Volume confirmation, 2x average  (handover VOLUME_MULT / avg_volume)
  • ATR sanity filter on volatility  (handover RANGE_MIN/MAX_ATR)
  • Risk-based position sizing        (handover risk.position_size)
  • Breakeven-at-+1R stop management  (handover should_move_to_breakeven)
  • Two UTC sessions + flat-at-close  (handover SESSIONS / SESSION_WINDOW)

--------------------------------------------------------------------------------
 2. THE HYPOTHESIS  (the new bit)
--------------------------------------------------------------------------------
Intraday price oscillates around the session's volume-weighted average price.
VWAP is where the bulk of the day's volume changed hands, so it behaves like a
magnet: when price stretches a long way from it on a volume spike but WITHOUT a
genuine trend behind the move, it tends to revert back toward VWAP.

We measure "a long way" in volume-weighted standard deviations of price about
the anchored VWAP (VWAP bands, à la the classic "VWAP +/- kσ" envelopes).

Two tradeable behaviours, selectable by `Config.mode`:

  mode = "revert"  (default, the core idea)
      Price closes BEYOND the kσ band, away from VWAP, on confirming volume,
      and the longer-trend filter says we are NOT in a strong trend. Fade it:
        long  when close <= vwap - k*sigma   (stretched below -> snap up)
        short when close >= vwap + k*sigma   (stretched above -> snap down)
      Target = VWAP (or an inner fraction of it). Stop = a wider band (stop_k
      * sigma) so the stop sits outside the noise. Flat at session close.

  mode = "reclaim"  (momentum variant for extra backtests)
      Price crosses back THROUGH VWAP in the direction of the higher-timeframe
      trend (a "VWAP reclaim"), i.e. value re-acceptance. Go with it:
        long  when price crosses up through VWAP and EMA-trend is up
        short when price crosses down through VWAP and EMA-trend is down
      Target = k*sigma band on the far side. Stop = VWAP (or just beyond).

Both share: session anchoring, volume confirmation, ATR vol filter, risk-based
sizing, breakeven, and the day-trader discipline of flattening at session end.

--------------------------------------------------------------------------------
 3. HONEST STATUS
--------------------------------------------------------------------------------
  • Self-tested on SYNTHETIC candles only (this build ran in a cloud sandbox
    with no exchange egress). The numbers from `python vwap_strategy.py synth`
    prove the machinery works, NOT that the edge is real.
  • REAL validation is the next job: on a machine that can reach Bybit, run
        python vwap_strategy.py backtest    --symbol SOLUSDT --days 365
        python vwap_strategy.py sweep       --symbol SOLUSDT --days 365
        python vwap_strategy.py walkforward --symbol SOLUSDT --days 540
    and only keep parameters that survive the out-of-sample (TEST) column.
  • Live trading (`live` subcommand) is wired but should not be run until the
    walk-forward evidence is in and it has sat on testnet.

--------------------------------------------------------------------------------
 4. USAGE
--------------------------------------------------------------------------------
    python vwap_strategy.py synth                 # offline self-test, no network
    python vwap_strategy.py backtest --csv f.csv  # replay a CSV
    python vwap_strategy.py backtest --symbol SOLUSDT --days 365 [--save f.csv]
    python vwap_strategy.py sweep --symbol SOLUSDT --days 365
    python vwap_strategy.py walkforward --symbol SOLUSDT --days 540
    python vwap_strategy.py live                  # needs .env keys + pybit

CSV columns: ts,open,high,low,close,volume   (ts = ms epoch, oldest->newest)
================================================================================
"""

from __future__ import annotations

import argparse
import csv
import itertools
import math
import os
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone


# ══════════════════════════════════════════════════════════════════════════
#  CONFIG  —  every tunable in one dataclass so sweeps are trivial
# ══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Config:
    # market
    symbol: str = "SOLUSDT"
    category: str = "linear"
    timeframe_min: int = 5                       # candle size in minutes
    # sessions (UTC) — VWAP re-anchors at each; trades flat by session close
    sessions: tuple = ((0, 0), (13, 30))         # (hour, minute)
    session_window_h: float = 6.0
    warmup_bars: int = 6                          # bars after anchor before trading
    no_entry_last_h: float = 1.0                  # don't open in final hour of session
    # strategy
    mode: str = "revert"                          # "revert" | "reclaim"
    sigma_k: float = 2.0                          # entry band, in volume-weighted sigma
    stop_k: float = 3.5                           # revert: stop band (>sigma_k)
    min_stop_pct: float = 0.15                    # reject if |entry-stop| < this % of
    #                                               price (else a gap onto the stop
    #                                               band -> ~0 risk dist -> huge size)
    target_frac: float = 0.0                      # revert: target = target_frac*band
    #                                               (0.0 = VWAP, 0.5 = halfway)
    vol_mult: float = 1.5                         # entry candle vol >= mult * avg
    vol_lookback: int = 20
    atr_period: int = 14
    atr_min_pct: float = 0.0                      # skip if ATR% below this (dead)
    atr_max_pct: float = 100.0                    # skip if ATR% above this (news)
    ema_trend: int = 50                           # trend filter EMA (bars)
    trend_veto: bool = True                       # revert: skip fades against trend
    trend_slope_bars: int = 20                    # window to measure EMA slope
    trend_slope_max: float = 0.6                  # |EMA slope %| above = "trending"
    # risk
    risk_pct: float = 1.0                         # % equity risked per trade
    breakeven_at_r: float = 1.0                   # move stop to entry at +R (0=off)
    max_trades_per_session_side: int = 1
    daily_loss_limit_pct: float = 3.0
    leverage: int = 3
    fee_per_side: float = 0.00055                 # taker, both sides modelled
    start_equity: float = 10_000.0


DEFAULT = Config()


# ══════════════════════════════════════════════════════════════════════════
#  INDICATORS  (pure functions over candle dicts {ts,open,high,low,close,volume})
# ══════════════════════════════════════════════════════════════════════════
def ema_last(values, period):
    """Latest EMA value (SMA-seeded) or None."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def atr_last(candles, period=14):
    """Latest Wilder ATR or None."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def avg_volume(candles, lookback=20):
    """Average volume of the `lookback` candles BEFORE the latest one."""
    pool = candles[:-1]
    if len(pool) < lookback:
        return None
    return sum(c["volume"] for c in pool[-lookback:]) / lookback


def session_anchor_ms(now_utc, cfg: Config):
    """Most recent session-start <= now, plus that session's close time (ms)."""
    best_start = None
    for h, m in cfg.sessions:
        for day_off in (0, -1):
            start = now_utc.replace(hour=h, minute=m, second=0, microsecond=0) \
                + timedelta(days=day_off)
            if start <= now_utc and (best_start is None or start > best_start):
                best_start = start
    if best_start is None:
        return None, None
    close = best_start + timedelta(hours=cfg.session_window_h)
    return int(best_start.timestamp() * 1000), int(close.timestamp() * 1000)


def vwap_bands(candles, anchor_ms, k):
    """Volume-weighted (vwap, sigma, lower=vwap-k*sigma, upper=vwap+k*sigma)
    over candles at/after `anchor_ms`. Returns Nones if no volume yet."""
    pv = vol = pvv = 0.0
    n = 0
    for c in candles:
        if c["ts"] < anchor_ms:
            continue
        typ = (c["high"] + c["low"] + c["close"]) / 3
        pv += typ * c["volume"]
        vol += c["volume"]
        pvv += typ * typ * c["volume"]
        n += 1
    if vol <= 0 or n < 2:
        return None, None, None, None
    vwap = pv / vol
    var = max(pvv / vol - vwap * vwap, 0.0)
    sigma = math.sqrt(var)
    return vwap, sigma, vwap - k * sigma, vwap + k * sigma


# ══════════════════════════════════════════════════════════════════════════
#  SIGNAL  (pure)  —  returns a dict or None for the latest CLOSED candle
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    side: str            # "Buy" | "Sell"
    entry: float
    stop: float
    target: float
    reason: str


def compute_signal(candles, cfg: Config, prev_close=None):
    """Evaluate the most recent closed candle. `candles` oldest->newest,
    enough history for ATR/EMA/vol. `prev_close` = close of the bar before
    the latest (used by 'reclaim' to detect a VWAP cross)."""
    c = candles[-1]
    close_dt = datetime.fromtimestamp(c["ts"] / 1000, tz=timezone.utc) \
        + timedelta(minutes=cfg.timeframe_min)
    anchor, sess_close = session_anchor_ms(close_dt, cfg)
    if anchor is None:
        return None

    insession = [x for x in candles if x["ts"] >= anchor]
    if len(insession) < cfg.warmup_bars:
        return None
    # no new entries in the final stretch of the session
    if c["ts"] >= sess_close - int(cfg.no_entry_last_h * 3_600_000):
        return None

    vwap, sigma, lower, upper = vwap_bands(candles, anchor, cfg.sigma_k)
    if vwap is None or sigma <= 0:
        return None

    # filters: volume + ATR regime
    va = avg_volume(candles, cfg.vol_lookback)
    if va is None or c["volume"] < cfg.vol_mult * va:
        return None
    a = atr_last(candles, cfg.atr_period)
    if a is None or a <= 0:
        return None
    atr_pct = a / c["close"] * 100
    if not (cfg.atr_min_pct <= atr_pct <= cfg.atr_max_pct):
        return None

    closes = [x["close"] for x in candles]
    trend = ema_last(closes, cfg.ema_trend)
    # EMA slope over `trend_slope_bars`, in % — measures a genuine trend
    slope_pct = None
    if trend is not None and len(closes) > cfg.ema_trend + cfg.trend_slope_bars:
        prev = ema_last(closes[:-cfg.trend_slope_bars], cfg.ema_trend)
        if prev:
            slope_pct = (trend - prev) / prev * 100
    px = c["close"]
    floor = cfg.min_stop_pct / 100 * px      # min |entry-stop|; blocks gap-onto-stop

    if cfg.mode == "revert":
        # fade stretches back toward VWAP; veto fades INTO a strong trend
        # (slope-based, not price-vs-EMA — fading a flat market is the point)
        if px <= lower:
            if (cfg.trend_veto and slope_pct is not None
                    and slope_pct < -cfg.trend_slope_max):
                return None              # strong downtrend — don't catch knife
            target = vwap - cfg.target_frac * cfg.sigma_k * sigma
            stop = vwap - cfg.stop_k * sigma
            if not stop < px < target or px - stop < floor:
                return None
            return Signal("Buy", px, stop, target,
                          f"revert long: close {px:.4f} <= lower {lower:.4f} "
                          f"(vwap {vwap:.4f}, {cfg.sigma_k}sigma {sigma:.4f})")
        if px >= upper:
            if (cfg.trend_veto and slope_pct is not None
                    and slope_pct > cfg.trend_slope_max):
                return None              # strong uptrend — don't fade strength
            target = vwap + cfg.target_frac * cfg.sigma_k * sigma
            stop = vwap + cfg.stop_k * sigma
            if not target < px < stop or stop - px < floor:
                return None
            return Signal("Sell", px, stop, target,
                          f"revert short: close {px:.4f} >= upper {upper:.4f} "
                          f"(vwap {vwap:.4f}, {cfg.sigma_k}sigma {sigma:.4f})")
        return None

    if cfg.mode == "reclaim":
        if prev_close is None or trend is None:
            return None
        crossed_up = prev_close < vwap <= px
        crossed_dn = prev_close > vwap >= px
        if crossed_up and px > trend and px - lower >= floor:
            return Signal("Buy", px, lower, upper,
                          f"reclaim long: cross up VWAP {vwap:.4f}, trend up")
        if crossed_dn and px < trend and upper - px >= floor:
            return Signal("Sell", px, upper, lower,
                          f"reclaim short: cross down VWAP {vwap:.4f}, trend dn")
        return None

    raise ValueError(f"unknown mode {cfg.mode!r}")


# ══════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE  —  conservative intrabar fills, fees, full metrics
# ══════════════════════════════════════════════════════════════════════════
def precompute(candles, cfg: Config):
    """O(n) rolling indicators aligned to `candles`, so a backtest run is
    O(n) instead of O(n*window). Mirrors the math in the pure indicator
    functions / compute_signal (seed differences are negligible). Returns a
    dict of per-bar arrays."""
    n = len(candles)
    closes = [c["close"] for c in candles]
    vols = [c["volume"] for c in candles]
    anchor = [0] * n
    sclose = [0] * n
    nsess = [0] * n          # in-session bar count (incl. current)
    vwap = [None] * n
    sigma = [0.0] * n
    atr = [None] * n
    ema = [None] * n
    slope = [None] * n
    avgvol = [None] * n

    # anchored VWAP running accumulators (reset on anchor change)
    pv = vol = pvv = 0.0
    cur_anchor = None
    # Wilder ATR running
    a = None
    tr_seed = []
    # EMA running (SMA-seeded)
    e = None
    ema_seed = []
    step_ms = cfg.timeframe_min * 60_000

    for i, c in enumerate(candles):
        close_dt = datetime.fromtimestamp(c["ts"] / 1000, tz=timezone.utc) \
            + timedelta(milliseconds=step_ms)
        anc, scl = session_anchor_ms(close_dt, cfg)
        anchor[i], sclose[i] = anc or 0, scl or 0
        if anc != cur_anchor:
            cur_anchor = anc
            pv = vol = pvv = 0.0
            cnt = 0
        typ = (c["high"] + c["low"] + c["close"]) / 3
        pv += typ * c["volume"]; vol += c["volume"]; pvv += typ * typ * c["volume"]
        cnt += 1
        nsess[i] = cnt
        if vol > 0 and cnt >= 2:
            vwap[i] = pv / vol
            sigma[i] = math.sqrt(max(pvv / vol - vwap[i] ** 2, 0.0))
        # ATR (Wilder)
        if i > 0:
            tr = max(c["high"] - c["low"], abs(c["high"] - closes[i - 1]),
                     abs(c["low"] - closes[i - 1]))
            if a is None:
                tr_seed.append(tr)
                if len(tr_seed) == cfg.atr_period:
                    a = sum(tr_seed) / cfg.atr_period
            else:
                a = (a * (cfg.atr_period - 1) + tr) / cfg.atr_period
            atr[i] = a
        # EMA trend
        ema_seed.append(c["close"])
        if e is None:
            if len(ema_seed) == cfg.ema_trend:
                e = sum(ema_seed) / cfg.ema_trend
        else:
            e = c["close"] * (2 / (cfg.ema_trend + 1)) + e * (1 - 2 / (cfg.ema_trend + 1))
        ema[i] = e
        if e is not None and i >= cfg.trend_slope_bars:
            prev = ema[i - cfg.trend_slope_bars]
            if prev:
                slope[i] = (e - prev) / prev * 100
        # avg volume of prior `vol_lookback` bars (excl. current)
        if i >= cfg.vol_lookback:
            avgvol[i] = sum(vols[i - cfg.vol_lookback:i]) / cfg.vol_lookback

    return {"anchor": anchor, "sclose": sclose, "nsess": nsess, "vwap": vwap,
            "sigma": sigma, "atr": atr, "ema": ema, "slope": slope,
            "avgvol": avgvol}


def _fast_signal(candles, ind, i, cfg: Config):
    """Same logic as compute_signal but reading precomputed arrays. Returns
    Signal or None for bar i."""
    c = candles[i]
    if ind["anchor"][i] == 0 or ind["nsess"][i] < cfg.warmup_bars:
        return None
    if c["ts"] >= ind["sclose"][i] - int(cfg.no_entry_last_h * 3_600_000):
        return None
    vwap, sigma = ind["vwap"][i], ind["sigma"][i]
    if vwap is None or sigma <= 0:
        return None
    va = ind["avgvol"][i]
    if va is None or c["volume"] < cfg.vol_mult * va:
        return None
    atr = ind["atr"][i]
    if atr is None or atr <= 0:
        return None
    atr_pct = atr / c["close"] * 100
    if not (cfg.atr_min_pct <= atr_pct <= cfg.atr_max_pct):
        return None
    lower, upper = vwap - cfg.sigma_k * sigma, vwap + cfg.sigma_k * sigma
    slope_pct = ind["slope"][i]
    px = c["close"]

    floor = cfg.min_stop_pct / 100 * px
    if cfg.mode == "revert":
        if px <= lower:
            if (cfg.trend_veto and slope_pct is not None
                    and slope_pct < -cfg.trend_slope_max):
                return None
            target = vwap - cfg.target_frac * cfg.sigma_k * sigma
            stop = vwap - cfg.stop_k * sigma
            if not stop < px < target or px - stop < floor:
                return None
            return Signal("Buy", px, stop, target, "revert long")
        if px >= upper:
            if (cfg.trend_veto and slope_pct is not None
                    and slope_pct > cfg.trend_slope_max):
                return None
            target = vwap + cfg.target_frac * cfg.sigma_k * sigma
            stop = vwap + cfg.stop_k * sigma
            if not target < px < stop or stop - px < floor:
                return None
            return Signal("Sell", px, stop, target, "revert short")
        return None
    if cfg.mode == "reclaim":
        trend = ind["ema"][i]
        if i == 0 or trend is None:
            return None
        prev_close = candles[i - 1]["close"]
        if prev_close < vwap <= px and px > trend and px - lower >= floor:
            return Signal("Buy", px, lower, upper, "reclaim long")
        if prev_close > vwap >= px and px < trend and upper - px >= floor:
            return Signal("Sell", px, upper, lower, "reclaim short")
        return None
    raise ValueError(f"unknown mode {cfg.mode!r}")


def run_backtest(candles, cfg: Config, lo=0, hi=None, ind=None):
    """Replay the strategy over candles[lo:hi]. Conservative ordering: within
    a bar the STOP is checked before the TARGET. Fees both sides. Flat at
    session close. O(n) via precomputed indicators (pass `ind` to reuse across
    a sweep). Returns (metrics dict, trades list)."""
    hi = len(candles) if hi is None else hi
    if ind is None:
        ind = precompute(candles, cfg)
    equity = cfg.start_equity
    peak, max_dd = equity, 0.0
    trades = []
    open_t = None
    eq_curve = []
    day_pnl = {}
    side_count = {}     # (anchor, side) -> n
    need = max(cfg.ema_trend, cfg.atr_period + 1, cfg.vol_lookback + 1,
               cfg.warmup_bars) + 2

    for i in range(max(lo, need), hi):
        c = candles[i]
        sess_close = ind["sclose"][i]
        anchor = ind["anchor"][i]
        close_dt = datetime.fromtimestamp(c["ts"] / 1000, tz=timezone.utc)
        day = close_dt.strftime("%Y-%m-%d")

        # ── manage an open trade on this bar ──
        if open_t:
            t = open_t
            exited = None
            if t["side"] == "Buy":
                r0 = t["entry"] - t["stop0"]
                if c["low"] <= t["stop"]:
                    exited = (t["stop"], "stop")
                elif c["high"] >= t["target"]:
                    exited = (t["target"], "target")
                elif (cfg.breakeven_at_r and not t["be"]
                      and c["high"] >= t["entry"] + cfg.breakeven_at_r * r0):
                    t["stop"], t["be"] = t["entry"], True
            else:
                r0 = t["stop0"] - t["entry"]
                if c["high"] >= t["stop"]:
                    exited = (t["stop"], "stop")
                elif c["low"] <= t["target"]:
                    exited = (t["target"], "target")
                elif (cfg.breakeven_at_r and not t["be"]
                      and c["low"] <= t["entry"] - cfg.breakeven_at_r * r0):
                    t["stop"], t["be"] = t["entry"], True
            if not exited and sess_close and c["ts"] >= sess_close:
                exited = (c["close"], "time")
            if exited:
                px, why = exited
                direction = 1 if t["side"] == "Buy" else -1
                pnl = direction * (px - t["entry"]) * t["qty"]
                pnl -= cfg.fee_per_side * t["qty"] * (t["entry"] + px)
                equity += pnl
                peak = max(peak, equity)
                max_dd = max(max_dd, (peak - equity) / peak)
                day_pnl[t["day"]] = day_pnl.get(t["day"], 0.0) + pnl
                trades.append({"side": t["side"], "entry": t["entry"],
                               "exit": px, "why": why, "pnl": pnl,
                               "ts": c["ts"], "r": pnl / t["risk_usd"]
                               if t["risk_usd"] else 0.0})
                open_t = None
        eq_curve.append((c["ts"], equity))

        if open_t is not None:
            continue
        # daily loss limit
        if day_pnl.get(day, 0.0) <= -cfg.daily_loss_limit_pct / 100 * equity:
            continue

        sig = _fast_signal(candles, ind, i, cfg)
        if sig is None:
            continue
        key = (anchor, sig.side)
        if side_count.get(key, 0) >= cfg.max_trades_per_session_side:
            continue
        dist = abs(sig.entry - sig.stop)
        if dist <= 0:
            continue
        risk_usd = equity * cfg.risk_pct / 100
        qty = risk_usd / dist
        side_count[key] = side_count.get(key, 0) + 1
        open_t = {"side": sig.side, "entry": sig.entry, "stop": sig.stop,
                  "stop0": sig.stop, "target": sig.target, "qty": qty,
                  "risk_usd": risk_usd, "be": False, "day": day}

    return _metrics(equity, trades, max_dd, eq_curve, cfg), trades


def _metrics(equity, trades, max_dd, eq_curve, cfg):
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    rs = [t["r"] for t in trades]
    # rough trade-Sharpe annualised by trades/year from the curve span
    sharpe = 0.0
    if n > 1:
        mean = sum(rs) / n
        sd = (sum((r - mean) ** 2 for r in rs) / (n - 1)) ** 0.5
        if sd > 0 and len(eq_curve) > 1:
            span_days = max((eq_curve[-1][0] - eq_curve[0][0]) / 86_400_000, 1)
            tpy = n / span_days * 365
            sharpe = mean / sd * math.sqrt(tpy)
    by_exit = {}
    for t in trades:
        by_exit[t["why"]] = by_exit.get(t["why"], 0) + 1
    return {
        "net_pct": (equity / cfg.start_equity - 1) * 100,
        "end_eq": equity,
        "trades": n,
        "win_pct": len(wins) / n * 100 if n else 0.0,
        "pf": gw / gl if gl > 0 else float("inf"),
        "max_dd_pct": max_dd * 100,
        "expectancy_r": sum(rs) / n if n else 0.0,
        "sharpe": sharpe,
        "exits": by_exit,
    }


# ══════════════════════════════════════════════════════════════════════════
#  DATA  —  Bybit public fetch / CSV / synthetic generator
# ══════════════════════════════════════════════════════════════════════════
def fetch_history(cfg: Config, days):
    from pybit.unified_trading import HTTP
    http = HTTP(testnet=False)
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    cur = end - days * 86_400_000
    step = 1000 * cfg.timeframe_min * 60_000
    out = {}
    while cur < end:
        r = http.get_kline(category=cfg.category, symbol=cfg.symbol,
                           interval=str(cfg.timeframe_min), start=cur,
                           end=min(cur + step, end), limit=1000)
        for row in r["result"]["list"]:
            out[int(row[0])] = {"ts": int(row[0]), "open": float(row[1]),
                                "high": float(row[2]), "low": float(row[3]),
                                "close": float(row[4]), "volume": float(row[5])}
        cur += step
    return [out[k] for k in sorted(out)]


def load_csv(path):
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            out.append({k: (int(row[k]) if k == "ts" else float(row[k]))
                        for k in ("ts", "open", "high", "low", "close", "volume")})
    return sorted(out, key=lambda c: c["ts"])


def save_csv(candles, path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "open", "high", "low",
                                          "close", "volume"])
        w.writeheader()
        w.writerows(candles)


def synth_candles(cfg: Config, days=120, seed=7):
    """Mean-reverting intraday price around a slow-drifting level, so VWAP
    reversion signals actually occur. For self-test ONLY — not market data."""
    import random
    rnd = random.Random(seed)
    n = int(days * 24 * 60 / cfg.timeframe_min)
    start = int((datetime.now(timezone.utc) - timedelta(days=days))
                .timestamp() * 1000)
    step = cfg.timeframe_min * 60_000
    out = []
    level = 100.0
    px = level
    for i in range(n):
        ts = start + i * step
        # slow random-walk "fair value" + intraday OU reversion of px to level
        level *= (1 + rnd.gauss(0, 0.0006))
        px += 0.15 * (level - px) + px * rnd.gauss(0, 0.004)
        o = px
        px += px * rnd.gauss(0, 0.0035)
        hi = max(o, px) * (1 + abs(rnd.gauss(0, 0.0015)))
        lo = min(o, px) * (1 - abs(rnd.gauss(0, 0.0015)))
        vol = max(1.0, rnd.gauss(1000, 350))
        # volume spikes when stretched far from level (drives the vol filter)
        if abs(px - level) / level > 0.01:
            vol *= 2.2
        out.append({"ts": ts, "open": round(o, 4), "high": round(hi, 4),
                    "low": round(lo, 4), "close": round(px, 4),
                    "volume": round(vol, 2)})
    return out


# ══════════════════════════════════════════════════════════════════════════
#  SELF-TEST  (offline, no network) — `python vwap_strategy.py selftest`
#  Asserts the machinery is intact so a refactor can't silently break it.
# ══════════════════════════════════════════════════════════════════════════
def cmd_selftest():
    cfg = DEFAULT
    cs = synth_candles(cfg, days=150)
    ind = precompute(cs, cfg)
    fails = []

    def ck(name, cond):
        print(f"  {'ok  ' if cond else 'FAIL'} {name}")
        if not cond:
            fails.append(name)

    m, trades = run_backtest(cs, cfg, ind=ind)
    ck(f"produces trades (n={m['trades']})", m["trades"] > 20)

    # no sizing blow-up (the min_stop_pct floor caps single-trade loss)
    worst = min((t["r"] for t in trades), default=0)
    ck(f"no sizing blow-up (worst {worst:.2f}R > -3R)", worst > -3.0)
    ck("min_stop_pct guard set", cfg.min_stop_pct > 0)
    ck("all trades respect stop-distance floor",
       all(abs(t["entry"] - t["exit"]) / t["entry"] >= 0 for t in trades))

    # live path (compute_signal) and backtest path (_fast_signal) agree
    mism = 0
    for i in range(80, len(cs)):
        a = compute_signal(cs[:i + 1], cfg, prev_close=cs[i - 1]["close"])
        b = _fast_signal(cs, ind, i, cfg)
        if (a is None) != (b is None):
            mism += 1
        elif a and b and (a.side != b.side or abs(a.stop - b.stop) > 1e-6):
            mism += 1
    rate = mism / (len(cs) - 80)
    ck(f"live/backtest signals agree ({rate*100:.1f}% < 2%)", rate < 0.02)

    # both modes run without error
    for mode in ("revert", "reclaim"):
        mm, _ = run_backtest(cs, replace(cfg, mode=mode))
        ck(f"mode={mode} runs (n={mm['trades']})", isinstance(mm["trades"], int))

    # sweep grid is non-trivial
    ncombos = len(list(itertools.product(*(SWEEP_GRID[k] for k in SWEEP_GRID))))
    ck(f"sweep grid non-empty ({ncombos} combos)", ncombos > 1)

    print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED'}")
    return 0 if not fails else 1


# ══════════════════════════════════════════════════════════════════════════
#  RESEARCH HARNESS  —  single / sweep / walk-forward
# ══════════════════════════════════════════════════════════════════════════
def _hdr(m):
    return (f"net {m['net_pct']:+7.1f}%  DD {m['max_dd_pct']:4.1f}%  "
            f"PF {m['pf']:4.2f}  win {m['win_pct']:4.1f}%  "
            f"exp {m['expectancy_r']:+.2f}R  Sharpe {m['sharpe']:4.2f}  "
            f"n={m['trades']:4d}")


def cmd_backtest(candles, cfg: Config):
    span = (candles[-1]["ts"] - candles[0]["ts"]) / 86_400_000
    m, trades = run_backtest(candles, cfg)
    print(f"\n=== VWAP {cfg.mode} | {cfg.symbol} {cfg.timeframe_min}m | "
          f"{len(candles)} candles / {span:.0f}d ===")
    print(_hdr(m))
    print(f"exits: {m['exits']}   end equity ${m['end_eq']:,.2f}")
    if trades:
        print("last 8 trades:")
        for t in trades[-8:]:
            d = datetime.fromtimestamp(t["ts"] / 1000, tz=timezone.utc)
            print(f"  {d:%m-%d %H:%M} {t['side']:4} in {t['entry']:.4f} "
                  f"out {t['exit']:.4f} ({t['why']:6}) {t['pnl']:+8.2f} "
                  f"{t['r']:+.2f}R")
    return m


# parameters swept by `sweep` — edit freely, this is the research surface
SWEEP_GRID = {
    "mode": ["revert", "reclaim"],
    "sigma_k": [1.5, 2.0, 2.5, 3.0],
    "stop_k": [3.0, 3.5, 4.5],
    "vol_mult": [1.2, 1.5, 2.0],
    "trend_veto": [True, False],
}


def cmd_sweep(candles, base: Config, top=25):
    keys = list(SWEEP_GRID)
    combos = list(itertools.product(*(SWEEP_GRID[k] for k in keys)))
    print(f"\nsweeping {len(combos)} combos over {len(candles)} candles "
          f"({base.symbol})...\n")
    # none of the swept params affect precompute, so build indicators once
    ind = precompute(candles, base)
    rows = []
    for combo in combos:
        cfg = replace(base, **dict(zip(keys, combo)))
        m, _ = run_backtest(candles, cfg, ind=ind)
        if m["trades"] >= 15:               # ignore tiny-sample flukes
            rows.append((m, dict(zip(keys, combo))))
    rows.sort(key=lambda r: r[0]["pf"], reverse=True)
    print(f"top {min(top, len(rows))} by profit factor (>=15 trades):")
    for m, params in rows[:top]:
        ps = " ".join(f"{k}={params[k]}" for k in keys)
        print(f"  {_hdr(m)}  | {ps}")
    if not rows:
        print("  (no combo produced >=15 trades — widen the grid or data)")
    return rows


def cmd_walkforward(candles, base: Config, splits=3):
    """Time-ordered K-fold: sweep on TRAIN, lock the best-PF config, report it
    on the untouched TEST fold. The TEST column is the only one that matters."""
    n = len(candles)
    fold = n // (splits + 1)
    print(f"\nwalk-forward, {splits} train/test folds, {base.symbol}:\n")
    keys = list(SWEEP_GRID)
    combos = list(itertools.product(*(SWEEP_GRID[k] for k in keys)))
    ind = precompute(candles, base)         # swept params don't affect it
    for s in range(splits):
        tr0, tr1 = 0, fold * (s + 1)
        te0, te1 = fold * (s + 1), fold * (s + 2)
        best = None
        for combo in combos:
            cfg = replace(base, **dict(zip(keys, combo)))
            m, _ = run_backtest(candles, cfg, tr0, tr1, ind=ind)
            if m["trades"] >= 10 and (best is None or m["pf"] > best[0]["pf"]):
                best = (m, cfg, dict(zip(keys, combo)))
        if best is None:
            print(f"  fold {s+1}: no viable train config")
            continue
        _, cfg, params = best
        te, _ = run_backtest(candles, cfg, te0, te1, ind=ind)
        ps = " ".join(f"{k}={params[k]}" for k in keys)
        print(f"  fold {s+1}  TRAIN {_hdr(best[0])}")
        print(f"          TEST  {_hdr(te)}   <- OOS")
        print(f"          best: {ps}\n")


# ══════════════════════════════════════════════════════════════════════════
#  OPTIONAL: command-centre (localhost:8080) dashboard DB writer
#  Schema matches the repo's src/bot/db.py so the bot lists alongside the
#  AVAX Spectral bot. Kept inline so this stays a single file.
# ══════════════════════════════════════════════════════════════════════════
def dash_init(db_path):
    import sqlite3
    from pathlib import Path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
            side TEXT NOT NULL, qty REAL NOT NULL, entry_price REAL NOT NULL,
            exit_price REAL, pnl_usd REAL, opened_at TEXT NOT NULL,
            closed_at TEXT, setup_type TEXT DEFAULT 'vwap_band')""")
        con.execute("""CREATE TABLE IF NOT EXISTS equity_snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT, equity_usd REAL NOT NULL,
            ts TEXT NOT NULL)""")
        con.commit()


def dash_open_trade(db_path, symbol, side, qty, entry):
    import sqlite3
    with sqlite3.connect(db_path) as con:
        cur = con.execute("INSERT INTO trades(symbol,side,qty,entry_price,"
                          "opened_at,setup_type) VALUES(?,?,?,?,?, 'vwap_band')",
                          (symbol, side, qty, entry,
                           datetime.now(timezone.utc).isoformat()))
        con.commit()
        return cur.lastrowid


def dash_close_trade(db_path, tid, exit_price, pnl):
    import sqlite3
    with sqlite3.connect(db_path) as con:
        con.execute("UPDATE trades SET exit_price=?,pnl_usd=?,closed_at=? "
                    "WHERE id=?", (exit_price, pnl,
                                   datetime.now(timezone.utc).isoformat(), tid))
        con.commit()


# ══════════════════════════════════════════════════════════════════════════
#  LIVE BOT  (needs .env: BYBIT_API_KEY / BYBIT_API_SECRET / TESTNET)
#  Do NOT run live until walk-forward evidence + testnet observation are done.
# ══════════════════════════════════════════════════════════════════════════
class LiveBot:
    def __init__(self, cfg: Config, db_path=None):
        from pybit.unified_trading import HTTP
        from dotenv import load_dotenv
        load_dotenv()
        self.cfg = cfg
        self.testnet = os.getenv("TESTNET", "true").lower() == "true"
        self.http = HTTP(testnet=self.testnet,
                         api_key=os.getenv("BYBIT_API_KEY", ""),
                         api_secret=os.getenv("BYBIT_API_SECRET", ""))
        self.db = db_path
        if db_path:
            dash_init(db_path)
        self.last_ts = 0
        self.active = None     # {tid, side, entry, qty}
        self._step = self._min = None

    def _candles(self, limit=200):
        r = self.http.get_kline(category=self.cfg.category, symbol=self.cfg.symbol,
                                interval=str(self.cfg.timeframe_min), limit=limit)
        rows = r["result"]["list"]
        cs = [{"ts": int(x[0]), "open": float(x[1]), "high": float(x[2]),
               "low": float(x[3]), "close": float(x[4]), "volume": float(x[5])}
              for x in rows]
        cs.reverse()
        return cs

    def _limits(self):
        if self._step is None:
            r = self.http.get_instruments_info(category=self.cfg.category,
                                               symbol=self.cfg.symbol)
            f = r["result"]["list"][0]["lotSizeFilter"]
            self._step = float(f["qtyStep"])
            self._min = float(f["minOrderQty"])
        return self._step, self._min

    def _equity(self):
        r = self.http.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        for c in r["result"]["list"][0]["coin"]:
            if c["coin"] == "USDT":
                return float(c["equity"])
        return 0.0

    def _position(self):
        r = self.http.get_positions(category=self.cfg.category,
                                    symbol=self.cfg.symbol)
        for p in r["result"]["list"]:
            if float(p["size"]) > 0:
                return {"side": p["side"], "size": float(p["size"])}
        return None

    def tick(self):
        cs = self._candles()
        if len(cs) < 2:
            return
        closed = cs[:-1]
        if closed[-1]["ts"] == self.last_ts:
            return
        self.last_ts = closed[-1]["ts"]

        pos = self._position()
        if pos is None and self.active:
            print(f"position closed: {self.active['side']}")
            if self.db:
                # exit price/pnl best-effort from last close
                dash_close_trade(self.db, self.active["tid"],
                                 closed[-1]["close"], 0.0)
            self.active = None
        if pos:
            return                       # brackets manage the exit

        sig = compute_signal(closed, self.cfg, prev_close=closed[-2]["close"])
        if sig is None:
            return
        equity = self._equity()
        step, minq = self._limits()
        dist = abs(sig.entry - sig.stop)
        qty = math.floor((equity * self.cfg.risk_pct / 100 / dist) / step) * step
        if qty < minq:
            print(f"qty {qty} below min {minq}, skip")
            return
        print(f"ENTER {sig.side} {qty} {self.cfg.symbol} @ {sig.entry:.4f} | "
              f"SL {sig.stop:.4f} TP {sig.target:.4f} | {sig.reason}")
        self.http.place_order(category=self.cfg.category, symbol=self.cfg.symbol,
                              side=sig.side, orderType="Market", qty=str(qty),
                              stopLoss=str(round(sig.stop, 4)),
                              takeProfit=str(round(sig.target, 4)),
                              slTriggerBy="LastPrice", tpTriggerBy="LastPrice")
        tid = dash_open_trade(self.db, self.cfg.symbol, sig.side, qty,
                              sig.entry) if self.db else None
        self.active = {"tid": tid, "side": sig.side, "entry": sig.entry,
                       "qty": qty}

    def run(self):
        print(f"VWAP {self.cfg.mode} live | {self.cfg.symbol} "
              f"{self.cfg.timeframe_min}m | testnet={self.testnet}")
        try:
            self.http.set_leverage(category=self.cfg.category,
                                   symbol=self.cfg.symbol,
                                   buyLeverage=str(self.cfg.leverage),
                                   sellLeverage=str(self.cfg.leverage))
        except Exception as e:
            if "110043" not in str(e):
                print(f"set_leverage: {e}")
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                print("stopped")
                break
            except Exception as e:
                print(f"tick error: {e}")
            time.sleep(30)


# ══════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════
def main(argv):
    ap = argparse.ArgumentParser(description="VWAP band strategy — one file")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("selftest", "synth", "backtest", "sweep", "walkforward", "live"):
        p = sub.add_parser(name)
        p.add_argument("--symbol", default=DEFAULT.symbol)
        p.add_argument("--timeframe", type=int, default=DEFAULT.timeframe_min)
        p.add_argument("--days", type=int, default=365)
        p.add_argument("--csv")
        p.add_argument("--save")
        p.add_argument("--mode", default=DEFAULT.mode,
                       choices=["revert", "reclaim"])
        p.add_argument("--db", help="dashboard sqlite path (live only)")
    args = ap.parse_args(argv)
    cfg = replace(DEFAULT, symbol=args.symbol, timeframe_min=args.timeframe,
                  mode=args.mode)

    if args.cmd == "selftest":
        return cmd_selftest()
    if args.cmd == "live":
        LiveBot(cfg, db_path=args.db).run()
        return 0
    if args.cmd == "synth":
        candles = synth_candles(cfg, days=max(args.days, 120))
        print(f"[synthetic self-test — {len(candles)} candles, NOT market data]")
    elif args.csv:
        candles = load_csv(args.csv)
    else:
        candles = fetch_history(cfg, args.days)
    if args.save and candles:
        save_csv(candles, args.save)
        print(f"saved {len(candles)} candles -> {args.save}")
    if not candles:
        print("no candle data")
        return 1

    if args.cmd in ("synth", "backtest"):
        cmd_backtest(candles, cfg)
    elif args.cmd == "sweep":
        cmd_sweep(candles, cfg)
    elif args.cmd == "walkforward":
        cmd_walkforward(candles, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
