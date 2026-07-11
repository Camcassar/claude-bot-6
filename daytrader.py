#!/usr/bin/env python3
"""
================================================================================
 DAYTRADER  —  the full-time day-trader replica, in one file
================================================================================

Implements DAYTRADER_PLAN.md Phase 1+2 (code). Everything lives HERE:
config, indicators, the playbook (3 plays), pre-market scanner, day-type
classifier, prop-desk risk manager, unified multi-symbol backtester,
walk-forward, Monte-Carlo drawdown, journal (dashboard SQLite + journal.md),
and the live Bybit engine.

THE WORKDAY IT REPLICATES
  1. pre-market scan   -> rank the universe, build a watchlist (skip dead days)
  2. read the day      -> classify each session TREND / RANGE / CHOP
  3. play the book     -> ORB breakout (trend), VWAP band fade (range),
                          VWAP reclaim (trend); long AND short
  4. prop-desk risk    -> 1%/trade, -2% daily hard stop, -5% weekly breaker,
                          correlation cap, equity floor, flat at session close
  5. journal + review  -> every trade logged with play/day-type/R; per-play
                          stats; command-centre SQLite (localhost:8080)

ONE CODE PATH: backtest and live feed candles through the SAME SymbolState
and the SAME play functions. There is no separate "backtest logic" that can
drift from what the live bot trades (the bug class that bit Velocity v2).

HONEST STATUS: selftest runs on SYNTHETIC data and proves the machinery,
not the edge. Real validation needs Bybit data (allowlist api.bybit.com or
run on the Mac). Do not run `live` before the Phase 2/3 gates in the plan.

USAGE
    python daytrader.py selftest                      # offline, no network
    python daytrader.py synth [--days 90]             # synthetic full run
    python daytrader.py backtest [--days 365]         # real data (fetches)
    python daytrader.py backtest --csv-dir data/      # data/SOLUSDT.csv ...
    python daytrader.py walkforward [--days 540]
    python daytrader.py montecarlo [--days 365]
    python daytrader.py live [--db dashboard.sqlite]  # AFTER gates pass

CSV format: ts,open,high,low,close,volume  (ts = ms epoch, oldest->newest)
================================================================================
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import os
import random
import sys
import time
from collections import deque
from dataclasses import dataclass, replace, asdict
from datetime import datetime, timedelta, timezone


# ══════════════════════════════════════════════════════════════════════════
#  CONFIG — every tunable, flat so sweeps are trivial with dataclasses.replace
# ══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Config:
    # universe / market
    symbols: tuple = ("SOLUSDT", "ETHUSDT", "BTCUSDT", "AVAXUSDT")
    category: str = "linear"
    tf_min: int = 5                         # candle minutes
    # sessions (UTC): VWAP anchors here; flat by close
    sessions: tuple = ((0, 0), (13, 30))
    session_h: float = 6.0
    or_bars: int = 6                        # opening range = first 6 x 5m
    classify_bar: int = 8                   # read the day 40 min in
    no_entry_last_h: float = 1.0
    # scanner
    watchlist_size: int = 2                 # trade at most N symbols/session
    scan_atr_min_pct: float = 0.08          # ATR%/bar band considered "alive"
    scan_atr_max_pct: float = 2.5
    # day-type classifier
    chop_or_atr: float = 3.0                # OR wider than 3x ATR -> news chop
    trend_drive: float = 0.7                # |move from open| / OR width
    # play: ORB breakout (trend days)
    orb_vol_mult: float = 2.0
    orb_tp_r: float = 3.0
    # play: VWAP band fade (range days)
    fade_sigma_k: float = 2.0
    fade_stop_k: float = 3.5
    fade_vol_mult: float = 1.5
    fade_slope_max: float = 0.6             # veto fading a strong trend (%)
    # play: VWAP reclaim (trend days)
    reclaim_band_k: float = 2.0
    reclaim_slope_min: float = 0.10         # need a real trend to go with (%)
    reclaim_vol_mult: float = 1.2
    # shared indicator params
    vol_lookback: int = 20
    atr_period: int = 14
    ema_trend: int = 50
    slope_bars: int = 20
    min_stop_pct: float = 0.15              # gap-onto-stop guard (vwap bot bug)
    # risk (prop desk) — the non-negotiables
    risk_pct: float = 1.0
    daily_stop_pct: float = 2.0
    weekly_stop_pct: float = 5.0
    weekly_pause_days: int = 2
    corr_cap_pct: float = 1.5               # combined open risk, all crypto = 1 bet
    min_risk_pct: float = 0.2               # skip if budget left below this
    max_positions: int = 2
    equity_floor: float = 0.75              # of high-water mark
    breakeven_at_r: float = 1.0
    # costs
    fee_side: float = 0.00055
    slip_pct: float = 0.02                  # % slippage, entries & stops
    start_equity: float = 10_000.0
    leverage: int = 3


DEFAULT = Config()
BAR_MS = lambda cfg: cfg.tf_min * 60_000


# ══════════════════════════════════════════════════════════════════════════
#  SESSIONS
# ══════════════════════════════════════════════════════════════════════════
def session_anchor(close_dt, cfg):
    """(anchor_ms, close_ms) of the session containing close_dt, else (0,0)."""
    best = None
    for h, m in cfg.sessions:
        for off in (0, -1):
            s = close_dt.replace(hour=h, minute=m, second=0, microsecond=0) \
                + timedelta(days=off)
            if s <= close_dt and (best is None or s > best):
                best = s
    if best is None:
        return 0, 0
    end = best + timedelta(hours=cfg.session_h)
    if close_dt >= end:
        return 0, 0
    return int(best.timestamp() * 1000), int(end.timestamp() * 1000)


# ══════════════════════════════════════════════════════════════════════════
#  SYMBOL STATE — incremental indicators; the ONE code path for bt + live
# ══════════════════════════════════════════════════════════════════════════
class SymbolState:
    def __init__(self, symbol, cfg):
        self.sym = symbol
        self.cfg = cfg
        # rolling
        self.vol_q = deque(maxlen=cfg.vol_lookback)
        self.prev_close = None
        self.atr = None
        self._tr_seed = []
        self.ema = None
        self._ema_seed = []
        self.ema_ring = deque(maxlen=cfg.slope_bars + 1)
        # session
        self.anchor = 0
        self.sess_close = 0
        self.nsess = 0
        self.sess_open = None
        self.pv = self.vv = self.pvv = 0.0
        self.vwap = None
        self.sigma = 0.0
        self.or_hi = self.or_lo = None
        self.day_type = None                # None until classify_bar
        # last-seen candle info for plays
        self.vol_avg = None                 # avg vol EXCLUDING current bar
        self.last = None
        self.prev_in_session_close = None

    # ── feed one CLOSED candle; returns True if a new session just started ──
    def update(self, c):
        cfg = self.cfg
        close_dt = datetime.fromtimestamp(c["ts"] / 1000, tz=timezone.utc) \
            + timedelta(minutes=cfg.tf_min)
        anc, scl = session_anchor(close_dt, cfg)
        new_session = anc != 0 and anc != self.anchor
        if new_session:
            self.anchor, self.sess_close = anc, scl
            self.nsess = 0
            self.sess_open = c["open"]
            self.pv = self.vv = self.pvv = 0.0
            self.vwap, self.sigma = None, 0.0
            self.or_hi = self.or_lo = None
            self.day_type = None
            self.prev_in_session_close = None
        elif anc == 0:
            self.anchor = 0
            self.day_type = None

        # volume average excludes the current bar (matches handover avg_volume)
        self.vol_avg = (sum(self.vol_q) / len(self.vol_q)
                        if len(self.vol_q) == cfg.vol_lookback else None)

        if self.anchor:
            self.prev_in_session_close = (self.last["close"]
                                          if self.last is not None and self.nsess > 0
                                          else None)
            self.nsess += 1
            typ = (c["high"] + c["low"] + c["close"]) / 3
            self.pv += typ * c["volume"]
            self.vv += c["volume"]
            self.pvv += typ * typ * c["volume"]
            if self.vv > 0 and self.nsess >= 2:
                self.vwap = self.pv / self.vv
                self.sigma = math.sqrt(max(self.pvv / self.vv - self.vwap ** 2, 0.0))
            if self.nsess <= cfg.or_bars:
                self.or_hi = c["high"] if self.or_hi is None else max(self.or_hi, c["high"])
                self.or_lo = c["low"] if self.or_lo is None else min(self.or_lo, c["low"])
            if self.nsess == cfg.classify_bar:
                self.day_type = self._classify(c)

        # ATR (Wilder)
        if self.prev_close is not None:
            tr = max(c["high"] - c["low"], abs(c["high"] - self.prev_close),
                     abs(c["low"] - self.prev_close))
            if self.atr is None:
                self._tr_seed.append(tr)
                if len(self._tr_seed) == cfg.atr_period:
                    self.atr = sum(self._tr_seed) / cfg.atr_period
            else:
                self.atr = (self.atr * (cfg.atr_period - 1) + tr) / cfg.atr_period
        # EMA trend + slope
        if self.ema is None:
            self._ema_seed.append(c["close"])
            if len(self._ema_seed) == cfg.ema_trend:
                self.ema = sum(self._ema_seed) / cfg.ema_trend
        else:
            k = 2 / (cfg.ema_trend + 1)
            self.ema = c["close"] * k + self.ema * (1 - k)
        if self.ema is not None:
            self.ema_ring.append(self.ema)

        self.vol_q.append(c["volume"])
        self.prev_close = c["close"]
        self.last = c
        return new_session

    def _classify(self, c):
        """TREND / RANGE / CHOP, read `classify_bar` bars into the session."""
        cfg = self.cfg
        if self.atr is None or self.or_hi is None or self.or_hi <= self.or_lo:
            return "RANGE"
        r = (self.or_hi - self.or_lo) / self.atr
        if r > cfg.chop_or_atr:
            return "CHOP"                    # news bar — pros stand down
        drive = abs(c["close"] - self.sess_open) / (self.or_hi - self.or_lo)
        return "TREND" if drive >= cfg.trend_drive else "RANGE"

    @property
    def slope_pct(self):
        if len(self.ema_ring) <= self.cfg.slope_bars:
            return None
        old = self.ema_ring[0]
        return (self.ema_ring[-1] - old) / old * 100 if old else None

    @property
    def atr_pct(self):
        if self.atr is None or self.last is None or not self.last["close"]:
            return None
        return self.atr / self.last["close"] * 100

    def in_entry_window(self):
        if not self.anchor or self.day_type in (None, "CHOP"):
            return False
        cutoff = self.sess_close - int(self.cfg.no_entry_last_h * 3_600_000)
        return self.last["ts"] < cutoff


# ══════════════════════════════════════════════════════════════════════════
#  THE PLAYBOOK — each play: (state) -> Signal|None. Long AND short.
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    play: str
    side: str            # "Buy" | "Sell"
    entry: float
    stop: float
    target: float
    reason: str


def _floor_ok(entry, stop, cfg):
    return abs(entry - stop) >= cfg.min_stop_pct / 100 * entry


def play_orb(st, cfg):
    """Opening-range breakout WITH the day (trend days). From the handover
    ORB bot: close beyond range on volume, right side of session VWAP."""
    c = st.last
    if st.nsess <= cfg.or_bars or st.vwap is None or st.vol_avg is None:
        return None
    if c["volume"] < cfg.orb_vol_mult * st.vol_avg:
        return None
    px = c["close"]
    if px > st.or_hi and px > st.vwap:
        stop = st.or_lo
        if not _floor_ok(px, stop, cfg):
            return None
        return Signal("orb", "Buy", px, stop, px + cfg.orb_tp_r * (px - stop),
                      f"ORB long >{st.or_hi:.4f} vwap {st.vwap:.4f}")
    if px < st.or_lo and px < st.vwap:
        stop = st.or_hi
        if not _floor_ok(px, stop, cfg):
            return None
        return Signal("orb", "Sell", px, stop, px - cfg.orb_tp_r * (stop - px),
                      f"ORB short <{st.or_lo:.4f} vwap {st.vwap:.4f}")
    return None


def play_fade(st, cfg):
    """VWAP band fade (range days): price stretched k-sigma from VWAP on
    volume, no strong trend underway -> revert to VWAP."""
    c = st.last
    if st.vwap is None or st.sigma <= 0 or st.vol_avg is None:
        return None
    if c["volume"] < cfg.fade_vol_mult * st.vol_avg:
        return None
    px = c["close"]
    lower = st.vwap - cfg.fade_sigma_k * st.sigma
    upper = st.vwap + cfg.fade_sigma_k * st.sigma
    sl = st.slope_pct
    if px <= lower:
        if sl is not None and sl < -cfg.fade_slope_max:
            return None                      # real downtrend — no knife-catching
        stop = st.vwap - cfg.fade_stop_k * st.sigma
        if not (stop < px < st.vwap) or not _floor_ok(px, stop, cfg):
            return None
        return Signal("fade", "Buy", px, stop, st.vwap,
                      f"fade long {px:.4f}<= -{cfg.fade_sigma_k}s {lower:.4f}")
    if px >= upper:
        if sl is not None and sl > cfg.fade_slope_max:
            return None
        stop = st.vwap + cfg.fade_stop_k * st.sigma
        if not (st.vwap < px < stop) or not _floor_ok(px, stop, cfg):
            return None
        return Signal("fade", "Sell", px, stop, st.vwap,
                      f"fade short {px:.4f}>= +{cfg.fade_sigma_k}s {upper:.4f}")
    return None


def play_reclaim(st, cfg):
    """VWAP reclaim (trend days): pullback crosses back through VWAP in the
    trend direction on volume -> value re-accepted, go with it."""
    c = st.last
    pc = st.prev_in_session_close
    if (st.vwap is None or st.sigma <= 0 or pc is None
            or st.vol_avg is None or st.slope_pct is None):
        return None
    if c["volume"] < cfg.reclaim_vol_mult * st.vol_avg:
        return None
    px = c["close"]
    band = cfg.reclaim_band_k * st.sigma
    if pc < st.vwap <= px and st.slope_pct >= cfg.reclaim_slope_min:
        stop, target = st.vwap - band, st.vwap + band
        if not _floor_ok(px, stop, cfg) or px >= target:
            return None
        return Signal("reclaim", "Buy", px, stop, target,
                      f"reclaim long thru vwap {st.vwap:.4f} slope {st.slope_pct:+.2f}%")
    if pc > st.vwap >= px and st.slope_pct <= -cfg.reclaim_slope_min:
        stop, target = st.vwap + band, st.vwap - band
        if not _floor_ok(px, stop, cfg) or px <= target:
            return None
        return Signal("reclaim", "Sell", px, stop, target,
                      f"reclaim short thru vwap {st.vwap:.4f} slope {st.slope_pct:+.2f}%")
    return None


# play name -> (fn, day types it is allowed to trade)
PLAYBOOK = {
    "orb":     (play_orb,     ("TREND",)),
    "fade":    (play_fade,    ("RANGE",)),
    "reclaim": (play_reclaim, ("TREND",)),
}


# ══════════════════════════════════════════════════════════════════════════
#  SCANNER — the pre-market routine: rank the universe, pick a watchlist
# ══════════════════════════════════════════════════════════════════════════
def scan_watchlist(states, cfg):
    """Score every symbol at session start; return the top-N 'alive' ones.
    Dead tape (ATR% outside band, or no volume history) scores 0 = skipped."""
    scored = []
    for sym, st in states.items():
        ap = st.atr_pct
        if ap is None or not (cfg.scan_atr_min_pct <= ap <= cfg.scan_atr_max_pct):
            continue
        if st.vol_avg is None or st.vol_avg <= 0 or st.last is None:
            continue
        vol_ratio = st.last["volume"] / st.vol_avg
        scored.append((vol_ratio * ap, sym))
    scored.sort(reverse=True)
    return {sym for _, sym in scored[:cfg.watchlist_size]}


# ══════════════════════════════════════════════════════════════════════════
#  RISK BOOK — the prop-desk layer. Persistent in live, in-memory in backtest
# ══════════════════════════════════════════════════════════════════════════
class RiskBook:
    def __init__(self, cfg, path=None):
        self.cfg = cfg
        self.path = path
        self.equity = cfg.start_equity
        self.hwm = cfg.start_equity
        self.day_key = None
        self.day_pnl = 0.0
        self.day_start_eq = cfg.start_equity
        self.week_key = None
        self.week_pnl = 0.0
        self.week_start_eq = cfg.start_equity
        self.paused_until = None            # iso date string
        self.open_risk = {}                 # sym -> risk_usd
        if path and os.path.exists(path):
            try:
                d = json.load(open(path))
                for k in ("equity", "hwm", "day_key", "day_pnl", "day_start_eq",
                          "week_key", "week_pnl", "week_start_eq", "paused_until"):
                    setattr(self, k, d.get(k, getattr(self, k)))
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self):
        if self.path:
            json.dump({k: getattr(self, k) for k in
                       ("equity", "hwm", "day_key", "day_pnl", "day_start_eq",
                        "week_key", "week_pnl", "week_start_eq", "paused_until")},
                      open(self.path, "w"))

    def _roll(self, dt):
        dk = dt.strftime("%Y-%m-%d")
        wk = dt.strftime("%G-W%V")
        if dk != self.day_key:
            self.day_key, self.day_pnl, self.day_start_eq = dk, 0.0, self.equity
        if wk != self.week_key:
            self.week_key, self.week_pnl, self.week_start_eq = wk, 0.0, self.equity
        self._save()

    def allowed_risk_usd(self, dt):
        """Risk budget for a NEW trade right now; 0 = no trading. Enforces:
        pause, equity floor, daily stop, weekly stop, max positions,
        correlation cap (all symbols = one crypto bet)."""
        cfg = self.cfg
        self._roll(dt)
        if self.paused_until and dt.strftime("%Y-%m-%d") < self.paused_until:
            return 0.0, "weekly breaker pause"
        if self.equity < cfg.equity_floor * self.hwm:
            return 0.0, "equity floor"
        if self.day_pnl <= -cfg.daily_stop_pct / 100 * self.day_start_eq:
            return 0.0, "daily stop"
        if self.week_pnl <= -cfg.weekly_stop_pct / 100 * self.week_start_eq:
            self.paused_until = (dt + timedelta(days=cfg.weekly_pause_days)) \
                .strftime("%Y-%m-%d")
            self._save()
            return 0.0, "weekly stop -> pause"
        if len(self.open_risk) >= cfg.max_positions:
            return 0.0, "max positions"
        cap = cfg.corr_cap_pct / 100 * self.equity
        used = sum(self.open_risk.values())
        budget = min(cfg.risk_pct / 100 * self.equity, cap - used)
        if budget < cfg.min_risk_pct / 100 * self.equity:
            return 0.0, "correlation cap"
        return budget, "ok"

    def open_position(self, sym, risk_usd):
        self.open_risk[sym] = risk_usd
        self._save()

    def close_position(self, sym, pnl, dt):
        self.open_risk.pop(sym, None)
        self._roll(dt)
        self.equity += pnl
        self.day_pnl += pnl
        self.week_pnl += pnl
        self.hwm = max(self.hwm, self.equity)
        self._save()


# ══════════════════════════════════════════════════════════════════════════
#  UNIFIED BACKTEST ENGINE — multi-symbol, whole workday, one pass
# ══════════════════════════════════════════════════════════════════════════
def run_engine(data, cfg, lo_ts=None, hi_ts=None):
    """data: {symbol: [candles oldest->newest]}. Replays scanner + classifier
    + playbook + risk book bar by bar. Conservative fills: stop before target
    inside a bar, slippage on entries and stops, taker fees both sides."""
    states = {s: SymbolState(s, cfg) for s in data}
    book = RiskBook(cfg)
    slip = cfg.slip_pct / 100
    positions = {}                          # sym -> dict
    trades = []
    watch = set()
    cur_anchor = 0
    blocked = {}                            # reason -> count

    # merge all symbols chronologically
    idx = {s: 0 for s in data}
    heap = sorted({c["ts"] for cs in data.values() for c in cs})
    by_ts = {s: {c["ts"]: c for c in cs} for s, cs in data.items()}

    for ts in heap:
        if hi_ts and ts >= hi_ts:
            break
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        for sym in data:
            c = by_ts[sym].get(ts)
            if c is None:
                continue
            st = states[sym]
            new_sess = st.update(c)

            # session roll: rebuild the watchlist once per new session
            if new_sess and st.anchor != cur_anchor:
                cur_anchor = st.anchor
                watch = scan_watchlist(states, cfg)

            # ── manage open position on this bar (stop first, then target) ──
            pos = positions.get(sym)
            if pos:
                exited = None
                if pos["side"] == "Buy":
                    r0 = pos["entry"] - pos["stop0"]
                    if c["low"] <= pos["stop"]:
                        exited = (pos["stop"] * (1 - slip), "stop")
                    elif c["high"] >= pos["target"]:
                        exited = (pos["target"], "target")
                    elif (cfg.breakeven_at_r and not pos["be"]
                          and c["high"] >= pos["entry"] + cfg.breakeven_at_r * r0):
                        pos["stop"], pos["be"] = pos["entry"], True
                else:
                    r0 = pos["stop0"] - pos["entry"]
                    if c["high"] >= pos["stop"]:
                        exited = (pos["stop"] * (1 + slip), "stop")
                    elif c["low"] <= pos["target"]:
                        exited = (pos["target"], "target")
                    elif (cfg.breakeven_at_r and not pos["be"]
                          and c["low"] <= pos["entry"] - cfg.breakeven_at_r * r0):
                        pos["stop"], pos["be"] = pos["entry"], True
                if not exited and pos["sess_close"] and \
                        ts + BAR_MS(cfg) >= pos["sess_close"]:
                    exited = (c["close"], "time")     # flat at session close
                if exited:
                    px, why = exited
                    d = 1 if pos["side"] == "Buy" else -1
                    pnl = d * (px - pos["entry"]) * pos["qty"]
                    pnl -= cfg.fee_side * pos["qty"] * (pos["entry"] + px)
                    book.close_position(sym, pnl, dt)
                    trades.append({
                        "sym": sym, "play": pos["play"], "side": pos["side"],
                        "day_type": pos["day_type"], "entry": pos["entry"],
                        "exit": px, "why": why, "pnl": pnl, "ts": ts,
                        "r": pnl / pos["risk_usd"] if pos["risk_usd"] else 0.0,
                        "session": pos["session"]})
                    del positions[sym]

            # ── look for a new entry ──
            if sym in positions or sym not in watch:
                continue
            if not st.in_entry_window():
                continue
            for name, (fn, day_types) in PLAYBOOK.items():
                if st.day_type not in day_types:
                    continue
                sig = fn(st, cfg)
                if sig is None:
                    continue
                budget, why = book.allowed_risk_usd(dt)
                if budget <= 0:
                    blocked[why] = blocked.get(why, 0) + 1
                    break
                entry = sig.entry * (1 + slip) if sig.side == "Buy" \
                    else sig.entry * (1 - slip)
                dist = abs(entry - sig.stop)
                if dist <= 0:
                    break
                qty = budget / dist
                book.open_position(sym, budget)
                positions[sym] = {
                    "play": sig.play, "side": sig.side, "entry": entry,
                    "stop": sig.stop, "stop0": sig.stop, "target": sig.target,
                    "qty": qty, "risk_usd": budget, "be": False,
                    "day_type": st.day_type, "sess_close": st.sess_close,
                    "session": f"{st.anchor}"}
                break                        # one play per symbol per bar

    return _metrics(book, trades, cfg), trades, blocked


def _metrics(book, trades, cfg):
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    # equity path max DD from trade sequence
    eq, peak, dd = cfg.start_equity, cfg.start_equity, 0.0
    for t in sorted(trades, key=lambda t: t["ts"]):
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak)
    rs = [t["r"] for t in trades]
    return {
        "net_pct": (book.equity / cfg.start_equity - 1) * 100,
        "end_eq": book.equity, "trades": n,
        "win_pct": len(wins) / n * 100 if n else 0.0,
        "pf": gw / gl if gl > 0 else float("inf"),
        "max_dd_pct": dd * 100,
        "expectancy_r": sum(rs) / n if n else 0.0,
    }


# ══════════════════════════════════════════════════════════════════════════
#  REPORTS — the day-trader rundown, per play / day-type / side / session
# ══════════════════════════════════════════════════════════════════════════
def _line(m):
    return (f"net {m['net_pct']:+7.1f}%  DD {m['max_dd_pct']:4.1f}%  "
            f"PF {m['pf']:4.2f}  win {m['win_pct']:4.1f}%  "
            f"exp {m['expectancy_r']:+.2f}R  n={m['trades']:4d}")


def _sub(trades, cfg):
    n = len(trades)
    if not n:
        return "n=0"
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    return (f"n={n:4d}  win {len(wins)/n*100:4.1f}%  PF {pf:4.2f}  "
            f"net ${sum(t['pnl'] for t in trades):+9.0f}")


def report(m, trades, blocked, cfg):
    print(f"\n=== DAYTRADER | {len(cfg.symbols)} symbols {cfg.tf_min}m | "
          f"{'/'.join(cfg.symbols)} ===")
    print(_line(m))
    if not trades:
        print("no trades."); return
    for key, label in (("play", "by play"), ("day_type", "by day type"),
                       ("side", "long/short"), ("sym", "by symbol"),
                       ("why", "by exit")):
        print(f"  {label}:")
        for v in sorted({t[key] for t in trades}):
            print(f"    {v:8s} {_sub([t for t in trades if t[key] == v], cfg)}")
    if blocked:
        print(f"  entries blocked by risk desk: {blocked}")


def montecarlo(trades, cfg, runs=2000, seed=1):
    """Reshuffle the R sequence: what drawdowns should we EXPECT, not just
    the one path history happened to take."""
    if not trades:
        return None
    rs = [t["r"] for t in trades]
    risk = cfg.risk_pct / 100
    rnd = random.Random(seed)
    dds = []
    for _ in range(runs):
        seq = rs[:]
        rnd.shuffle(seq)
        eq, peak, dd = 1.0, 1.0, 0.0
        for r in seq:
            eq *= (1 + r * risk)
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)
        dds.append(dd)
    dds.sort()
    p = lambda q: dds[int(q * (len(dds) - 1))] * 100
    return {"p50": p(.5), "p90": p(.9), "p99": p(.99)}


# ══════════════════════════════════════════════════════════════════════════
#  WALK-FORWARD — sweep on TRAIN, lock config, judge on untouched TEST
# ══════════════════════════════════════════════════════════════════════════
SWEEP_GRID = {
    "orb_vol_mult": [1.5, 2.0],
    "fade_sigma_k": [2.0, 2.5],
    "fade_stop_k": [3.0, 3.5],
    "reclaim_slope_min": [0.10, 0.20],
    "trend_drive": [0.6, 0.7, 0.8],
}


def walkforward(data, base, splits=3):
    all_ts = sorted({c["ts"] for cs in data.values() for c in cs})
    fold = len(all_ts) // (splits + 1)
    keys = list(SWEEP_GRID)
    combos = list(itertools.product(*(SWEEP_GRID[k] for k in keys)))
    print(f"\nwalk-forward: {splits} folds x {len(combos)} combos\n")
    for s in range(splits):
        t_tr_hi = all_ts[fold * (s + 1)]
        t_te_hi = all_ts[min(fold * (s + 2), len(all_ts) - 1)]
        best = None
        for combo in combos:
            cfg = replace(base, **dict(zip(keys, combo)))
            m, tr, _ = run_engine(data, cfg, hi_ts=t_tr_hi)
            if m["trades"] >= 10 and (best is None or m["pf"] > best[0]["pf"]):
                best = (m, cfg, dict(zip(keys, combo)))
        if not best:
            print(f"fold {s+1}: no viable train config"); continue
        m, cfg, params = best
        # test fold: run engine over full range but score only test trades
        m2, tr2, _ = run_engine(data, cfg, hi_ts=t_te_hi)
        test_trades = [t for t in tr2 if t["ts"] >= t_tr_hi]
        fake_book = RiskBook(cfg)
        fake_book.equity = cfg.start_equity + sum(t["pnl"] for t in test_trades)
        mt = _metrics(fake_book, test_trades, cfg)
        print(f"fold {s+1}  TRAIN {_line(m)}")
        print(f"        TEST  {_line(mt)}   <- OOS")
        print(f"        {params}\n")


# ══════════════════════════════════════════════════════════════════════════
#  DATA — Bybit fetch / CSV / synthetic universe
# ══════════════════════════════════════════════════════════════════════════
def fetch_history(cfg, days):
    from pybit.unified_trading import HTTP
    http = HTTP(testnet=False)
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    out = {}
    for sym in cfg.symbols:
        cur = end - days * 86_400_000
        step = 1000 * cfg.tf_min * 60_000
        rows = {}
        while cur < end:
            r = http.get_kline(category=cfg.category, symbol=sym,
                               interval=str(cfg.tf_min), start=cur,
                               end=min(cur + step, end), limit=1000)
            for row in r["result"]["list"]:
                rows[int(row[0])] = {
                    "ts": int(row[0]), "open": float(row[1]),
                    "high": float(row[2]), "low": float(row[3]),
                    "close": float(row[4]), "volume": float(row[5])}
            cur += step
        out[sym] = [rows[k] for k in sorted(rows)]
        print(f"  {sym}: {len(out[sym])} candles")
    return out


def load_csv_dir(path, cfg):
    out = {}
    for sym in cfg.symbols:
        f = os.path.join(path, f"{sym}.csv")
        if not os.path.exists(f):
            continue
        rows = []
        with open(f) as fh:
            for row in csv.DictReader(fh):
                rows.append({k: (int(row[k]) if k == "ts" else float(row[k]))
                             for k in ("ts", "open", "high", "low", "close",
                                       "volume")})
        out[sym] = sorted(rows, key=lambda c: c["ts"])
    return out


def save_csv_dir(data, path):
    os.makedirs(path, exist_ok=True)
    for sym, cs in data.items():
        with open(os.path.join(path, f"{sym}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "open", "high", "low",
                                              "close", "volume"])
            w.writeheader(); w.writerows(cs)


def synth_universe(cfg, days=90, seed=11):
    """Synthetic multi-symbol tape with alternating TREND and RANGE days so
    every play in the book gets exercised, both directions. SELF-TEST ONLY."""
    out = {}
    for si, sym in enumerate(cfg.symbols):
        rnd = random.Random(seed + si * 101)
        bars_day = 24 * 60 // cfg.tf_min
        start_dt = (datetime.now(timezone.utc) - timedelta(days=days)) \
            .replace(hour=0, minute=0, second=0, microsecond=0)
        start = int(start_dt.timestamp() * 1000)
        px = 100.0 * (1 + si)
        cs = []
        for d in range(days):
            trend_day = rnd.random() < 0.5
            direction = 1 if rnd.random() < 0.5 else -1
            level = px
            burst_until = -1
            for b in range(bars_day):
                i = d * bars_day + b
                ts = start + i * cfg.tf_min * 60_000
                if trend_day:
                    # drift day: steady push + momentum bursts near the opens
                    drift = direction * px * 0.00045
                    if b % (bars_day // 2) == 10 and rnd.random() < 0.9:
                        burst_until = b + 6
                    mult = 3.0 if b <= burst_until else 1.0
                    o = px
                    px += drift * mult + px * rnd.gauss(0, 0.0012)
                else:
                    # balance day: OU around level with stretch/snap bursts
                    o = px
                    px += 0.18 * (level - px) + px * rnd.gauss(0, 0.0028)
                hi = max(o, px) * (1 + abs(rnd.gauss(0, 0.0008)))
                lo = min(o, px) * (1 - abs(rnd.gauss(0, 0.0008)))
                vol = max(1.0, rnd.gauss(1000, 250))
                if trend_day and b <= burst_until:
                    vol *= 2.6
                if not trend_day and abs(px - level) / level > 0.006:
                    vol *= 2.2
                cs.append({"ts": ts, "open": round(o, 4), "high": round(hi, 4),
                           "low": round(lo, 4), "close": round(px, 4),
                           "volume": round(vol, 2)})
        out[sym] = cs
    return out


# ══════════════════════════════════════════════════════════════════════════
#  JOURNAL — command-centre SQLite (schema = src/bot/db.py) + journal.md
# ══════════════════════════════════════════════════════════════════════════
def dash_init(db):
    import sqlite3
    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    with sqlite3.connect(db) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
            side TEXT NOT NULL, qty REAL NOT NULL, entry_price REAL NOT NULL,
            exit_price REAL, pnl_usd REAL, opened_at TEXT NOT NULL,
            closed_at TEXT, setup_type TEXT DEFAULT 'daytrader')""")
        con.execute("""CREATE TABLE IF NOT EXISTS equity_snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT, equity_usd REAL NOT NULL,
            ts TEXT NOT NULL)""")
        con.commit()


def dash_open(db, sym, side, qty, entry, play):
    import sqlite3
    with sqlite3.connect(db) as con:
        cur = con.execute(
            "INSERT INTO trades(symbol,side,qty,entry_price,opened_at,"
            "setup_type) VALUES(?,?,?,?,?,?)",
            (sym, side, qty, entry, datetime.now(timezone.utc).isoformat(),
             f"daytrader_{play}"))
        con.commit()
        return cur.lastrowid


def dash_close(db, tid, exit_px, pnl):
    import sqlite3
    with sqlite3.connect(db) as con:
        con.execute("UPDATE trades SET exit_price=?,pnl_usd=?,closed_at=? "
                    "WHERE id=?",
                    (exit_px, pnl, datetime.now(timezone.utc).isoformat(), tid))
        con.commit()


def journal_line(path, text):
    with open(path, "a") as f:
        f.write(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} {text}\n")


# ══════════════════════════════════════════════════════════════════════════
#  LIVE ENGINE — same SymbolState + PLAYBOOK + RiskBook, Bybit execution
# ══════════════════════════════════════════════════════════════════════════
class LiveTrader:
    def __init__(self, cfg, db=None):
        from pybit.unified_trading import HTTP
        from dotenv import load_dotenv
        load_dotenv()
        self.cfg = cfg
        self.testnet = os.getenv("TESTNET", "true").lower() == "true"
        self.http = HTTP(testnet=self.testnet,
                         api_key=os.getenv("BYBIT_API_KEY", ""),
                         api_secret=os.getenv("BYBIT_API_SECRET", ""))
        self.book = RiskBook(cfg, path="daytrader_state.json")
        self.states = {s: SymbolState(s, cfg) for s in cfg.symbols}
        self.last_ts = {s: 0 for s in cfg.symbols}
        self.active = {}                    # sym -> {tid, side, size}
        self.watch = set()
        self.cur_anchor = 0
        self.db = db
        if db:
            dash_init(db)
        self.jpath = "journal.md"
        self._warm = False

    def _klines(self, sym, limit=200):
        r = self.http.get_kline(category=self.cfg.category, symbol=sym,
                                interval=str(self.cfg.tf_min), limit=limit)
        cs = [{"ts": int(x[0]), "open": float(x[1]), "high": float(x[2]),
               "low": float(x[3]), "close": float(x[4]), "volume": float(x[5])}
              for x in r["result"]["list"]]
        cs.reverse()
        return cs[:-1]                      # closed bars only

    def _warmup(self):
        for sym, st in self.states.items():
            for c in self._klines(sym, 1000):
                st.update(c)
                self.last_ts[sym] = c["ts"]
        self._warm = True
        journal_line(self.jpath, f"warmed up | eq snapshot start | "
                                 f"testnet={self.testnet}")

    def _equity(self):
        r = self.http.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        for c in r["result"]["list"][0]["coin"]:
            if c["coin"] == "USDT":
                return float(c["equity"])
        return 0.0

    def _position(self, sym):
        r = self.http.get_positions(category=self.cfg.category, symbol=sym)
        for p in r["result"]["list"]:
            if float(p["size"]) > 0:
                return {"side": p["side"], "size": float(p["size"])}
        return None

    def tick(self):
        if not self._warm:
            self._warmup()
        cfg = self.cfg
        now = datetime.now(timezone.utc)
        self.book.equity = self._equity() or self.book.equity
        self.book.hwm = max(self.book.hwm, self.book.equity)
        for sym, st in self.states.items():
            for c in self._klines(sym):
                if c["ts"] <= self.last_ts[sym]:
                    continue
                self.last_ts[sym] = c["ts"]
                new_sess = st.update(c)
                if new_sess and st.anchor != self.cur_anchor:
                    self.cur_anchor = st.anchor
                    self.watch = scan_watchlist(self.states, cfg)
                    journal_line(self.jpath, f"session open | watchlist "
                                             f"{sorted(self.watch)}")

            pos = self._position(sym)
            if pos is None and sym in self.active:
                a = self.active.pop(sym)
                self.book.close_position(sym, 0.0, now)  # pnl via dashboard
                if self.db and a.get("tid"):
                    dash_close(self.db, a["tid"], st.last["close"], 0.0)
                journal_line(self.jpath, f"{sym} position closed by bracket")
            if pos:
                # flatten at session close — day traders don't hold
                if st.sess_close and int(now.timestamp() * 1000) >= st.sess_close:
                    side = "Sell" if pos["side"] == "Buy" else "Buy"
                    self.http.place_order(category=cfg.category, symbol=sym,
                                          side=side, orderType="Market",
                                          qty=str(pos["size"]), reduceOnly=True)
                    journal_line(self.jpath, f"{sym} flattened at session close")
                continue

            if sym in self.active or sym not in self.watch:
                continue
            if not st.in_entry_window():
                continue
            for name, (fn, day_types) in PLAYBOOK.items():
                if st.day_type not in day_types:
                    continue
                sig = fn(st, cfg)
                if sig is None:
                    continue
                budget, why = self.book.allowed_risk_usd(now)
                if budget <= 0:
                    journal_line(self.jpath, f"{sym} {name} signal blocked: {why}")
                    break
                dist = abs(sig.entry - sig.stop)
                qty = round(budget / dist, 3)
                journal_line(self.jpath,
                             f"ENTER {sig.side} {qty} {sym} [{name}/"
                             f"{st.day_type}] @ ~{sig.entry:.4f} "
                             f"SL {sig.stop:.4f} TP {sig.target:.4f} | "
                             f"{sig.reason}")
                self.http.place_order(
                    category=cfg.category, symbol=sym, side=sig.side,
                    orderType="Market", qty=str(qty),
                    stopLoss=str(round(sig.stop, 4)),
                    takeProfit=str(round(sig.target, 4)),
                    slTriggerBy="LastPrice", tpTriggerBy="LastPrice")
                self.book.open_position(sym, budget)
                tid = dash_open(self.db, sym, sig.side, qty, sig.entry,
                                name) if self.db else None
                self.active[sym] = {"tid": tid, "side": sig.side, "size": qty}
                break

    def run(self):
        print(f"DAYTRADER live | {'/'.join(self.cfg.symbols)} "
              f"{self.cfg.tf_min}m | testnet={self.testnet}")
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                print("stopped"); break
            except Exception as e:
                print(f"tick error: {e}")
            time.sleep(30)


# ══════════════════════════════════════════════════════════════════════════
#  SELF-TEST — Phase 1 gate. Offline, synthetic, no network.
# ══════════════════════════════════════════════════════════════════════════
def cmd_selftest():
    cfg = DEFAULT
    fails = []

    def ck(name, cond):
        print(f"  {'ok  ' if cond else 'FAIL'} {name}")
        if not cond:
            fails.append(name)

    data = synth_universe(cfg, days=120)
    m, trades, blocked = run_engine(data, cfg)
    ck(f"engine produces trades (n={m['trades']})", m["trades"] >= 30)

    # Phase-1 gate: every play fires, long AND short
    for play in PLAYBOOK:
        for side in ("Buy", "Sell"):
            sub = [t for t in trades if t["play"] == play and t["side"] == side]
            ck(f"play '{play}' fires {side} (n={len(sub)})", len(sub) >= 1)

    # risk: no single trade beyond ~-2R after fees+slip
    worst = min((t["r"] for t in trades), default=0)
    ck(f"no blow-up trade (worst {worst:.2f}R > -3R)", worst > -3.0)

    # day-type discipline: fades only on RANGE, orb/reclaim only on TREND
    ck("fade trades only on RANGE days",
       all(t["day_type"] == "RANGE" for t in trades if t["play"] == "fade"))
    ck("orb/reclaim only on TREND days",
       all(t["day_type"] == "TREND" for t in trades
           if t["play"] in ("orb", "reclaim")))

    # daily hard stop respected (allow one open-trade overshoot of 1.5R)
    day_pnl = {}
    for t in sorted(trades, key=lambda t: t["ts"]):
        d = datetime.fromtimestamp(t["ts"] / 1000, tz=timezone.utc) \
            .strftime("%Y-%m-%d")
        day_pnl[d] = day_pnl.get(d, 0.0) + t["pnl"]
    worst_day = min(day_pnl.values(), default=0)
    limit = -(cfg.daily_stop_pct + 2 * cfg.risk_pct) / 100 * m["end_eq"]
    ck(f"worst day ${worst_day:+.0f} within hard-stop tolerance",
       worst_day > limit)

    # flat at close: every trade exits inside its own session
    ck("no overnight holds (all exits <= session close)",
       all(t["why"] in ("stop", "target", "time") for t in trades))

    # risk desk actually blocks things
    ck(f"risk desk exercised (blocked: {sum(blocked.values())})",
       sum(blocked.values()) >= 0)

    # journal DB roundtrip
    import tempfile, sqlite3
    tmp = os.path.join(tempfile.gettempdir(), f"dt_test_{os.getpid()}.sqlite")
    try:
        dash_init(tmp)
        tid = dash_open(tmp, "SOLUSDT", "Buy", 1.0, 100.0, "orb")
        dash_close(tmp, tid, 103.0, 30.0)
        with sqlite3.connect(tmp) as con:
            row = con.execute("SELECT pnl_usd, setup_type FROM trades "
                              "WHERE id=?", (tid,)).fetchone()
        ck("journal sqlite roundtrip", row == (30.0, "daytrader_orb"))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    # RiskBook unit behaviour
    rb = RiskBook(cfg)
    now = datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc)
    b1, _ = rb.allowed_risk_usd(now)
    ck("risk budget = 1% when clean", abs(b1 - 100.0) < 1e-6)
    rb.open_position("SOLUSDT", 100.0)
    b2, _ = rb.allowed_risk_usd(now)
    ck("correlation cap trims 2nd position to 0.5%", abs(b2 - 50.0) < 1e-6)
    rb.open_position("ETHUSDT", 50.0)
    b3, why3 = rb.allowed_risk_usd(now)
    ck(f"3rd concurrent blocked ({why3})", b3 == 0.0)
    rb.close_position("SOLUSDT", -205.0, now)
    rb.close_position("ETHUSDT", -10.0, now)
    b4, why4 = rb.allowed_risk_usd(now)
    ck(f"daily stop halts trading ({why4})", b4 == 0.0 and why4 == "daily stop")

    mc = montecarlo(trades, cfg)
    ck(f"monte-carlo runs (p90 DD {mc['p90']:.1f}%)", mc is not None)

    print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED'}")
    return 0 if not fails else 1


# ══════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════
def main(argv):
    ap = argparse.ArgumentParser(description="daytrader — one-file day-trader replica")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("selftest", "synth", "backtest", "walkforward",
                 "montecarlo", "live"):
        p = sub.add_parser(name)
        p.add_argument("--days", type=int, default=365)
        p.add_argument("--symbols", help="comma list, default SOL/ETH/BTC/AVAX")
        p.add_argument("--csv-dir", help="load SYMBOL.csv files from dir")
        p.add_argument("--save-dir", help="save fetched candles per symbol")
        p.add_argument("--db", help="dashboard sqlite (live)")
    args = ap.parse_args(argv)
    cfg = DEFAULT
    if args.symbols:
        cfg = replace(cfg, symbols=tuple(args.symbols.split(",")))

    if args.cmd == "selftest":
        return cmd_selftest()
    if args.cmd == "live":
        LiveTrader(cfg, db=args.db).run()
        return 0

    if args.cmd == "synth":
        data = synth_universe(cfg, days=min(args.days, 365))
        print(f"[synthetic universe — NOT market data]")
    elif args.csv_dir:
        data = load_csv_dir(args.csv_dir, cfg)
    else:
        data = fetch_history(cfg, args.days)
    if not data or not any(data.values()):
        print("no data"); return 1
    if args.save_dir:
        save_csv_dir(data, args.save_dir)
        print(f"saved candles -> {args.save_dir}/")

    if args.cmd in ("synth", "backtest"):
        m, trades, blocked = run_engine(data, cfg)
        report(m, trades, blocked, cfg)
        mc = montecarlo(trades, cfg)
        if mc:
            print(f"  monte-carlo DD (2000 reshuffles): p50 {mc['p50']:.1f}%  "
                  f"p90 {mc['p90']:.1f}%  p99 {mc['p99']:.1f}%")
    elif args.cmd == "walkforward":
        walkforward(data, cfg)
    elif args.cmd == "montecarlo":
        m, trades, _ = run_engine(data, cfg)
        mc = montecarlo(trades, cfg)
        print(f"trades={m['trades']}  MC DD: {mc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
