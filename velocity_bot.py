"""
CumRSI2-Regime | ETHUSDT 1D — Cam's live Bybit bot.

Signal: Cumulative RSI(2) two-day sum < 55 while close > SMA-150 (uptrend regime).
Exit:   RSI(2) > 70 signal close at daily bar close, or server-side SL at 4.5x ATR(14).
        A wide 8x ATR TP rides as a crash-safe ceiling bracket.
Risk:   Full-equity sizing at 3x leverage (SIZING_MODE=risk switches to 4%-risk sizing).

Backtest (Jan 2022 - Jul 2026, Bybit ETHUSDT.P 1d, unlevered):
  +114.5% net, PF 2.90, WR 71.1%, DD -16.4%, 45 trades.
  Walk-forward: H1 +17.7% / H2 +82.2%. Wiggle-test stable (+/-20% params all positive).
"""

import csv
import logging
import os
import sys
import time
from math import floor

import requests

import config
from exchange import Bybit
from indicators import atr_series, rsi_series, sma_series

SYMBOL   = os.getenv("SYMBOL", "ETHUSDT")
BOT_NAME = f"CumRSI2-Regime | {SYMBOL} 1D"

CUM_TH      = float(os.getenv("CUM_TH", "55"))    # cum RSI(2) two-day entry threshold
EXIT_RSI    = float(os.getenv("EXIT_RSI", "70"))  # RSI(2) exit threshold
STOP_ATR    = float(os.getenv("STOP_ATR", "4.5")) # SL distance in ATR(14)
TP_ATR      = float(os.getenv("TP_ATR", "8.0"))   # safety ceiling bracket, rarely hit
SMA_LEN     = int(os.getenv("SMA_LEN", "150"))    # regime filter
RSI_LEN     = 2
ATR_LEN     = 14

FUNDING_TH  = float(os.getenv("FUNDING_TH", "0.0008"))  # skip entry when 24h funding sum exceeds this (crowded longs)

SIZING_MODE = os.getenv("SIZING_MODE", "full")    # "full" = equity x leverage; "risk" = RISK_PCT via stop distance
RISK_PCT    = float(os.getenv("RISK_PCT", "4.0"))
NOTIONAL_HEADROOM = 0.95                          # keep fee/margin headroom in full mode

POLL_SECONDS   = 60
CANDLES_NEEDED = SMA_LEN + ATR_LEN + 20
TRADE_LOG = os.getenv("TRADE_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "trades.csv"))
CB_LOSSES     = 4
CB_PAUSE_BARS = 7   # daily bars

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [CumRSI2]: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cumrsi2")

# ── Telegram ──────────────────────────────────────────────────────────
_TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def tg(msg: str) -> None:
    """Fire-and-forget Telegram message. Silently drops on failure."""
    if not _TG_TOKEN or not _TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
            json={"chat_id": _TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception:
        pass


# ── Trade log ─────────────────────────────────────────────────────────
_LOG_FIELDS = ["timestamp", "type", "direction", "qty", "price", "tp", "sl", "equity", "cum_rsi", "pnl", "loss_streak"]


def _log_trade(row: dict) -> None:
    write_header = not os.path.exists(TRADE_LOG)
    try:
        os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
        with open(TRADE_LOG, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_LOG_FIELDS)
            if write_header:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in _LOG_FIELDS})
    except Exception as e:
        log.warning("trade log write failed: %s", e)


# ── Signal ────────────────────────────────────────────────────────────
def compute_state(candles):
    """Returns dict with rsi, cum_rsi, sma, atr, regime_up, entry, exit for the latest closed bar."""
    closes = [c["close"] for c in candles]
    if len(closes) < CANDLES_NEEDED:
        return None

    rsi = rsi_series(closes, RSI_LEN)
    sma = sma_series(closes, SMA_LEN)
    atr = atr_series(candles, ATR_LEN)
    if len(rsi) < 2 or not sma or not atr:
        return None

    cum = rsi[-1] + rsi[-2]
    regime_up = closes[-1] > sma[-1]
    return {
        "close":     closes[-1],
        "rsi":       rsi[-1],
        "cum_rsi":   cum,
        "sma":       sma[-1],
        "atr":       atr[-1],
        "regime_up": regime_up,
        "entry":     cum < CUM_TH and regime_up,
        "exit":      rsi[-1] > EXIT_RSI,
    }


# ── Bot ───────────────────────────────────────────────────────────────
class CumRsi2Bot:
    def __init__(self):
        self.ex = Bybit(symbol=SYMBOL)
        self.last_bar = 0
        self.loss_streak = 0
        self.pause_until_bar = 0
        self.had_position = False
        self.last_pnl_ts = int(time.time() * 1000)
        self.last_report_hour = -1
        self._last_state = None
        self._last_regime = None  # True=up, False=down, None=unknown

    def _check_regime_flip(self, state):
        if state is None:
            return
        regime = state["regime_up"]
        if self._last_regime is not None and regime != self._last_regime:
            if regime:
                tg(
                    f"🟢 *{BOT_NAME}* — REGIME FLIPPED UP\n"
                    f"Close `{state['close']:.2f}` > SMA{SMA_LEN} `{state['sma']:.2f}`\n"
                    f"Dip-buying is now ARMED — entry fires when cum RSI(2) < {CUM_TH:.0f}\n"
                    f"Cum RSI(2) now: `{state['cum_rsi']:.1f}`"
                )
            else:
                tg(
                    f"🔴 *{BOT_NAME}* — REGIME FLIPPED DOWN\n"
                    f"Close `{state['close']:.2f}` < SMA{SMA_LEN} `{state['sma']:.2f}`\n"
                    f"Standing aside — no new entries until regime is UP again"
                )
        self._last_regime = regime

    def _track_results(self):
        try:
            for rec in sorted(self.ex.get_closed_pnl(), key=lambda r: r["ts"]):
                if rec["ts"] > self.last_pnl_ts:
                    self.last_pnl_ts = rec["ts"]
                    pnl = rec["pnl"]
                    if pnl < 0:
                        self.loss_streak += 1
                        log.info("loss booked %.2f USDT | streak=%d", pnl, self.loss_streak)
                        tg(f"❌ *{BOT_NAME}*\nTrade closed: `{pnl:+.2f} USDT`\nLoss streak: {self.loss_streak}")
                        if self.loss_streak >= CB_LOSSES:
                            self.pause_until_bar = self.last_bar + CB_PAUSE_BARS * 86_400_000
                            msg = f"⚠️ *{BOT_NAME}* — CIRCUIT BREAKER\n{CB_LOSSES} straight losses. Pausing {CB_PAUSE_BARS} days."
                            log.warning(msg)
                            tg(msg)
                    else:
                        self.loss_streak = 0
                        tg(f"✅ *{BOT_NAME}*\nTrade closed: `+{pnl:.2f} USDT`")
                    _log_trade({
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rec["ts"] / 1000)),
                        "type": "EXIT",
                        "pnl": round(pnl, 4),
                        "loss_streak": self.loss_streak,
                    })
        except Exception as e:
            log.warning("pnl tracking failed: %s", e)

    def _send_status(self, pos):
        try:
            candles = self.ex.get_candles(limit=CANDLES_NEEDED + 5)
            state = compute_state(candles[:-1])
            if state:
                self._last_state = state
                self._check_regime_flip(state)
        except Exception:
            pass
        state = self._last_state

        if state:
            regime_str = "UP ✅" if state["regime_up"] else "DOWN ⛔"
            cum_str = f"`{state['cum_rsi']:.1f}` (entry < {CUM_TH:.0f})"
        else:
            regime_str = "unknown"
            cum_str = "unavailable"

        if pos:
            status_str = f"📈 LONG position held — SL bracket active, exit on RSI(2) > {EXIT_RSI:.0f}"
        elif self.last_bar < self.pause_until_bar:
            status_str = "⚠️ Circuit breaker active — paused"
        elif state and state["entry"]:
            status_str = "🔥 Signal firing — will enter on next daily close!"
        else:
            status_str = "Watching — waiting for oversold dip in uptrend"

        try:
            equity = self.ex.get_equity()
            equity_str = f"`{equity:.2f} USDT`"
        except Exception:
            equity_str = "unavailable"

        msg = (
            f"📊 *{BOT_NAME}*\n"
            f"Balance: {equity_str}\n"
            f"Cum RSI(2): {cum_str}\n"
            f"Regime (SMA{SMA_LEN}): {regime_str}\n"
            f"{status_str}"
        )
        log.info("hourly status | pos=%s", bool(pos))
        tg(msg)

    def _position_qty(self, equity, px, atr):
        qty_step, min_qty = self.ex.get_instrument_limits()
        if SIZING_MODE == "risk":
            stop_dist = STOP_ATR * atr
            qty = (RISK_PCT / 100 * equity) / stop_dist
        else:
            qty = (equity * config.LEVERAGE * NOTIONAL_HEADROOM) / px
        qty = floor(round(qty / qty_step, 9)) * qty_step
        return qty, min_qty

    def tick(self):
        now_hour = int(time.time()) // 3600
        if now_hour != self.last_report_hour:
            self.last_report_hour = now_hour
            try:
                pos_for_status = self.ex.get_position()
            except Exception:
                pos_for_status = None
            self._send_status(pos_for_status)

        # near-real-time win/loss alerts (bracket fills happen intra-day)
        self._track_results()

        candles = self.ex.get_candles(limit=CANDLES_NEEDED + 5)
        if len(candles) < CANDLES_NEEDED + 1:
            log.warning("not enough candles yet (%d)", len(candles))
            return

        closed = candles[:-1]
        bar_ts = closed[-1]["ts"]
        if bar_ts == self.last_bar:
            return
        self.last_bar = bar_ts

        state = compute_state(closed)
        self._last_state = state
        if state is None:
            log.warning("indicator warmup incomplete")
            return

        pos = self.ex.get_position()

        # exit first: RSI(2) snap-back close at daily bar close
        if pos:
            self.had_position = True
            if state["exit"]:
                log.info("EXIT signal | rsi=%.1f > %.0f — closing position", state["rsi"], EXIT_RSI)
                self.ex.close_position(pos["side"], pos["size"])
                tg(
                    f"🔁 *{BOT_NAME}* — EXIT SIGNAL, SELL PLACED\n"
                    f"Closing `{pos['size']}` {SYMBOL} @ ~`{state['close']:.2f}`\n"
                    f"RSI(2) `{state['rsi']:.1f}` > {EXIT_RSI:.0f} — snap-back complete\n"
                    f"P&L confirmation follows once the fill settles"
                )
            return

        if self.had_position:
            log.info("position closed (bracket or signal)")
            self.had_position = False

        if bar_ts < self.pause_until_bar:
            log.info("circuit breaker active — skipping")
            return

        if not state["entry"]:
            log.info("no entry | cum=%.1f regime_up=%s", state["cum_rsi"], state["regime_up"])
            return

        try:
            funding = self.ex.get_daily_funding()
        except Exception as e:
            log.warning("funding fetch failed (%s) — allowing entry", e)
            funding = 0.0
        if funding > FUNDING_TH:
            log.info("entry BLOCKED by funding filter | 24h funding %.4f%% > %.4f%%",
                     funding * 100, FUNDING_TH * 100)
            tg(
                f"🚫 *{BOT_NAME}* — dip signal skipped\n"
                f"24h funding `{funding * 100:.3f}%` > `{FUNDING_TH * 100:.2f}%` (crowded longs)\n"
                f"Cum RSI(2) `{state['cum_rsi']:.1f}` — waiting for a cleaner dip"
            )
            return

        equity = self.ex.get_equity()
        px = state["close"]
        qty, min_qty = self._position_qty(equity, px, state["atr"])
        if qty < min_qty:
            log.warning("qty %.4f below minimum, skipping", qty)
            return

        sl = px - STOP_ATR * state["atr"]
        tp = px + TP_ATR * state["atr"]

        log.info("ENTER LONG %.4f @ ~%.2f | TP %.2f SL %.2f | equity %.2f | cum=%.1f",
                 qty, px, tp, sl, equity, state["cum_rsi"])

        self.ex.market_order("Buy", qty, stop_loss=sl, take_profit=tp)
        self.had_position = True

        ts_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tg(
            f"📈 *{BOT_NAME}* — TRADE ENTERED\n"
            f"*LONG* `{qty}` {SYMBOL}\n"
            f"Entry: `{px:.2f}` | TP: `{tp:.2f}` | SL: `{sl:.2f}`\n"
            f"Cum RSI(2): `{state['cum_rsi']:.1f}` | Sizing: `{SIZING_MODE}` @ `{config.LEVERAGE}x` of `{equity:.2f} USDT`"
        )
        _log_trade({
            "timestamp": ts_now,
            "type": "ENTRY",
            "direction": "LONG",
            "qty": qty,
            "price": px,
            "tp": round(tp, 4),
            "sl": round(sl, 4),
            "equity": round(equity, 2),
            "cum_rsi": round(state["cum_rsi"], 2),
        })

    def run(self):
        startup = (
            f"🚀 *{BOT_NAME}* — LIVE\n"
            f"Symbol: {SYMBOL} | TF: 1D\n"
            f"Entry: cum RSI(2) < {CUM_TH:.0f} + close > SMA{SMA_LEN}\n"
            f"Exit: RSI(2) > {EXIT_RSI:.0f} | SL: {STOP_ATR:.1f}x ATR{ATR_LEN}\n"
            f"Sizing: {SIZING_MODE} @ {config.LEVERAGE}x | Testnet: {config.TESTNET}"
        )
        log.info(startup.replace("*", "").replace("`", ""))
        tg(startup)
        self.ex.set_leverage()

        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                log.info("stopped by user")
                break
            except Exception as e:
                log.error("tick error: %s", e, exc_info=True)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if not config.API_KEY or not config.API_SECRET:
        sys.exit("Set BYBIT_API_KEY and BYBIT_API_SECRET in .env first.")
    CumRsi2Bot().run()
