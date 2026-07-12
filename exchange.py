"""
Exchange — thin Bybit v5 wrapper (pybit unified trading).
All API calls live here; strategy stays testable offline.
"""

import logging

from pybit.unified_trading import HTTP

import config

log = logging.getLogger("exchange")


class Bybit:
    def __init__(self, symbol=None):
        self.http = HTTP(
            testnet=config.TESTNET,
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
        )
        self.symbol = symbol or config.SYMBOL
        self._qty_step = None
        self._min_qty = None

    # ── market data ──────────────────────────────────────────────
    def get_candles(self, limit=None):
        """Fetch closed + forming candle, oldest -> newest."""
        r = self.http.get_kline(
            category=config.CATEGORY,
            symbol=self.symbol,
            interval=config.TIMEFRAME,
            limit=limit or config.CANDLE_LIMIT,
        )
        rows = r["result"]["list"]  # newest first from Bybit
        candles = [
            {
                "ts":     int(row[0]),
                "open":   float(row[1]),
                "high":   float(row[2]),
                "low":    float(row[3]),
                "close":  float(row[4]),
                "volume": float(row[5]),
            }
            for row in rows
        ]
        candles.reverse()
        return candles

    def get_last_price(self):
        r = self.http.get_tickers(category=config.CATEGORY, symbol=self.symbol)
        return float(r["result"]["list"][0]["lastPrice"])

    def get_instrument_limits(self):
        """Return (qty_step, min_qty) cached."""
        if self._qty_step is None:
            r = self.http.get_instruments_info(
                category=config.CATEGORY, symbol=self.symbol
            )
            f = r["result"]["list"][0]["lotSizeFilter"]
            self._qty_step = float(f["qtyStep"])
            self._min_qty  = float(f["minOrderQty"])
        return self._qty_step, self._min_qty

    # ── account ──────────────────────────────────────────────────
    def get_equity(self):
        r = self.http.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        for c in r["result"]["list"][0]["coin"]:
            if c["coin"] == "USDT":
                return float(c["equity"])
        return 0.0

    def get_position(self):
        """Return open position dict or None."""
        r = self.http.get_positions(category=config.CATEGORY, symbol=self.symbol)
        for p in r["result"]["list"]:
            if float(p["size"]) > 0:
                return {
                    "side":           p["side"],
                    "size":           float(p["size"]),
                    "entry":          float(p["avgPrice"]),
                    "unrealized_pnl": float(p["unrealisedPnl"]),
                    "stop_loss":      float(p["stopLoss"]) if p["stopLoss"] else None,
                    "take_profit":    float(p["takeProfit"]) if p["takeProfit"] else None,
                }
        return None

    def set_leverage(self):
        try:
            self.http.set_leverage(
                category=config.CATEGORY,
                symbol=self.symbol,
                buyLeverage=str(config.LEVERAGE),
                sellLeverage=str(config.LEVERAGE),
            )
        except Exception as e:
            if "110043" not in str(e):  # 110043 = already set, harmless
                log.warning("set_leverage: %s", e)

    # ── orders ───────────────────────────────────────────────────
    def market_order(self, side, qty, stop_loss, take_profit):
        """Market entry with server-side SL/TP so a crash never leaves an unprotected position."""
        return self.http.place_order(
            category=config.CATEGORY,
            symbol=self.symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            stopLoss=str(round(stop_loss, 4)),
            takeProfit=str(round(take_profit, 4)),
            slTriggerBy="LastPrice",
            tpTriggerBy="LastPrice",
        )

    def close_position(self, side, size):
        """Reduce-only market close."""
        opposite = "Sell" if side == "Buy" else "Buy"
        return self.http.place_order(
            category=config.CATEGORY,
            symbol=self.symbol,
            side=opposite,
            orderType="Market",
            qty=str(size),
            reduceOnly=True,
        )

    def get_daily_funding(self):
        """Sum of the last 3 funding rates (= last 24h on 8h funding)."""
        r = self.http.get_funding_rate_history(
            category=config.CATEGORY, symbol=self.symbol, limit=3
        )
        return sum(float(row["fundingRate"]) for row in r["result"]["list"])

    def get_closed_pnl(self, limit=10):
        """Recent closed-trade PnL for this symbol (circuit breaker tracking)."""
        r = self.http.get_closed_pnl(
            category=config.CATEGORY, symbol=self.symbol, limit=limit
        )
        return [
            {"ts": int(row["updatedTime"]), "pnl": float(row["closedPnl"])}
            for row in r["result"]["list"]
        ]

    def get_all_closed_pnl(self, limit=50):
        """All closed trades across all symbols (for trade log viewer)."""
        r = self.http.get_closed_pnl(category=config.CATEGORY, limit=limit)
        return r["result"]["list"]
