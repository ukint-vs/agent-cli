"""Autoresearch champion — 9-signal ensemble with SDO-adaptive ATR stops.

Synced to S4 champion (score 31.36, expanded universe).
Signals: momentum(12), micro-momentum(6), EMA(5/23), RSI(4), MACD(7/30/2),
  BB compression(6), RSI divergence(8), Donchian 5-bar 70%, 3-bar micro momentum.
Exits: ATR trailing stop (5.5x, tightened to 1.85x by SDO at 85/15), RSI mean-reversion, signal flip.

Live adaptations:
  - Hourly candle aggregation (ticks → 1h bars, bootstrapped from HL API)
  - USD-based dynamic position sizing (tvl × BASE_POSITION_PCT × weight × leverage)
  - Signals fire on bar close only; exits checked every tick
"""
from __future__ import annotations

import math
import time as _time
from collections import deque
from typing import List, Optional

import requests

from common.models import MarketSnapshot, StrategyDecision
from sdk.strategy_sdk.base import BaseStrategy, StrategyContext

# --- Signal parameters (S4 champion, synced from auto-researchtrading/strategy.py) ---
SHORT_WINDOW = 6
MED_WINDOW = 12
LONG_WINDOW = 36
EMA_FAST = 5
EMA_SLOW = 23
RSI_PERIOD = 8          # used for divergence detection
RSI_ENTRY_PERIOD = 4    # used for entry/exit signal (hardcoded in backtest)
RSI_BULL = 50
RSI_BEAR = 50
RSI_OVERBOUGHT = 74
RSI_OVERSOLD = 26
MACD_FAST = 7
MACD_SLOW = 30
MACD_SIGNAL = 2
BB_PERIOD = 6
BASE_THRESHOLD = 0.013
BASE_POSITION_PCT = 0.058
VOL_LOOKBACK = 42
TARGET_VOL = 0.015
ATR_LOOKBACK = 12
ATR_STOP_MULT = 5.5
SDO_STOCH_LEN = 10
SDO_DONCH_LEN = 14
SDO_SMOOTH_LEN = 3
SDO_OVERBOUGHT = 85
SDO_OVERSOLD = 15
SDO_TIGHT_ATR_MULT = 1.85
COOLDOWN_BARS = 0
MIN_VOTES = 5  # out of 9
DONCH_PERIOD = 5
DONCH_PCT = 0.70
RSI_DIV_LOOKBACK = 14

MIN_HISTORY = max(LONG_WINDOW, EMA_SLOW + 10, MACD_SLOW + MACD_SIGNAL + 5,
                  BB_PERIOD * 3, SDO_STOCH_LEN, SDO_DONCH_LEN, ATR_LOOKBACK,
                  VOL_LOOKBACK, RSI_DIV_LOOKBACK + RSI_PERIOD) + 5

# HL size decimals per coin (for order rounding)
SZ_DECIMALS = {"BTC": 5, "ETH": 4, "SOL": 2, "XRP": 0, "DOGE": 0, "FARTCOIN": 0}
HL_MIN_NOTIONAL = 10.0

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


# --- Pure Python helpers (no numpy) ---

def _ema(values: list, span: int) -> list:
    alpha = 2.0 / (span + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(alpha * v + (1 - alpha) * result[-1])
    return result


def _calc_rsi(closes: list, period: int) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(len(closes) - period, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss < 1e-10:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _calc_macd(closes: list) -> float:
    n = MACD_SLOW + MACD_SIGNAL + 5
    if len(closes) < n:
        return 0.0
    seg = closes[-n:]
    fast = _ema(seg, MACD_FAST)
    slow = _ema(seg, MACD_SLOW)
    macd_line = [f - s for f, s in zip(fast, slow)]
    signal_line = _ema(macd_line, MACD_SIGNAL)
    return macd_line[-1] - signal_line[-1]


def _calc_bb_width_pctile(closes: list, period: int) -> float:
    if len(closes) < period * 3:
        return 50.0
    widths = []
    for i in range(period * 2, len(closes)):
        window = closes[i - period:i]
        sma = sum(window) / period
        if sma <= 0:
            widths.append(0.0)
            continue
        variance = sum((x - sma) ** 2 for x in window) / period
        std = math.sqrt(variance)
        widths.append(2 * std / sma)
    if len(widths) < 2:
        return 50.0
    current = widths[-1]
    return 100.0 * sum(1 for w in widths if w <= current) / len(widths)


def _calc_atr(highs: list, lows: list, closes: list, lookback: int) -> Optional[float]:
    if len(closes) < lookback + 1:
        return None
    trs = []
    for i in range(-lookback, 0):
        h, l, prev_c = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(trs) / len(trs)


def _calc_vol(closes: list, lookback: int) -> float:
    if len(closes) < lookback + 1:
        return TARGET_VOL
    log_rets = [math.log(closes[i] / closes[i - 1])
                for i in range(len(closes) - lookback, len(closes))
                if closes[i - 1] > 0 and closes[i] > 0]
    if len(log_rets) < 2:
        return TARGET_VOL
    mean = sum(log_rets) / len(log_rets)
    var = sum((r - mean) ** 2 for r in log_rets) / len(log_rets)
    return max(math.sqrt(var), 1e-6)


def _calc_sdo(highs: list, lows: list, closes: list) -> float:
    """Simplified SDO — returns last value only.

    Matches backtest: stoch 0-100, donch -50..+50, combined without offset.
    """
    n = len(closes)
    start = max(SDO_STOCH_LEN, SDO_DONCH_LEN)
    if n <= start:
        return 50.0

    # Stochastic component (0-100)
    hh = max(highs[-SDO_STOCH_LEN:])
    ll = min(lows[-SDO_STOCH_LEN:])
    rng = hh - ll
    stoch = ((closes[-1] - ll) / rng * 100) if rng > 1e-10 else 50.0

    # Donchian component (-50..+50)
    hh_d = max(highs[-SDO_DONCH_LEN:])
    ll_d = min(lows[-SDO_DONCH_LEN:])
    rng_d = hh_d - ll_d
    mid_d = (hh_d + ll_d) / 2
    donch = ((closes[-1] - mid_d) / rng_d * 100) if rng_d > 1e-10 else 0.0

    return 0.5 * stoch + 0.5 * donch


def _calc_rsi_divergence(closes: list, period: int, lookback: int) -> tuple:
    """Returns (rsi, has_bull_div, has_bear_div)."""
    rsi_val = _calc_rsi(closes, period)
    has_bull = False
    has_bear = False
    if len(closes) < lookback + period + 1:
        return rsi_val, has_bull, has_bear

    # RSI series over lookback
    rsi_series = []
    for i in range(lookback):
        idx = len(closes) - lookback + i
        rsi_series.append(_calc_rsi(closes[:idx + 1], period))

    price_arr = closes[-lookback:]
    for i in range(1, lookback - 1):
        # Bull: price lower low, RSI higher low
        if price_arr[i] < price_arr[i - 1] and price_arr[i] < price_arr[i + 1]:
            if price_arr[-1] < price_arr[i] and rsi_series[-1] > rsi_series[i]:
                has_bull = True
        # Bear: price higher high, RSI lower high
        if price_arr[i] > price_arr[i - 1] and price_arr[i] > price_arr[i + 1]:
            if price_arr[-1] > price_arr[i] and rsi_series[-1] < rsi_series[i]:
                has_bear = True

    return rsi_val, has_bull, has_bear


# --- Candle Aggregator ---

class CandleAggregator:
    """Accumulates ticks into hourly OHLCV bars aligned to UTC hour boundaries."""

    def __init__(self, max_bars: int = 500):
        self.closes: list = []
        self.highs: list = []
        self.lows: list = []
        self._bars: deque = deque(maxlen=max_bars)
        self._current: Optional[dict] = None
        self._last_hour: Optional[int] = None

    def bootstrap(self, coin: str) -> None:
        """Fetch historical 1h candles from HL API to fill the buffer."""
        end_ms = int(_time.time() * 1000)
        start_ms = end_ms - 500 * 3_600_000  # 500 hours back
        all_rows = []
        current = start_ms
        chunk_ms = 30 * 24 * 3_600_000

        while current < end_ms:
            body = {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": "1h",
                    "startTime": current,
                    "endTime": min(current + chunk_ms, end_ms),
                },
            }
            try:
                resp = requests.post(HL_INFO_URL, json=body, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                if not data:
                    current += chunk_ms
                    continue
                for c in data:
                    all_rows.append({
                        "open": float(c["o"]),
                        "high": float(c["h"]),
                        "low": float(c["l"]),
                        "close": float(c["c"]),
                        "ts": int(c["t"]),
                    })
                current = int(data[-1]["t"]) + 3_600_000
            except Exception:
                current += chunk_ms
            _time.sleep(0.2)

        # Deduplicate and sort
        seen = set()
        for bar in sorted(all_rows, key=lambda b: b["ts"]):
            if bar["ts"] not in seen:
                seen.add(bar["ts"])
                self._bars.append(bar)

        self._rebuild_arrays()
        if self._bars:
            self._last_hour = self._bars[-1]["ts"] // 3_600_000

    def update(self, mid: float, high: float, low: float, timestamp_ms: int) -> bool:
        """Add a tick. Returns True if a new hourly bar just closed."""
        hour = timestamp_ms // 3_600_000

        if self._last_hour is None:
            self._last_hour = hour
            self._current = {"open": mid, "high": high, "low": low, "close": mid, "ts": hour * 3_600_000}
            return False

        if hour != self._last_hour:
            # Close previous bar
            if self._current is not None:
                self._bars.append(self._current)
                self._rebuild_arrays()
            # Open new bar
            self._current = {"open": mid, "high": high, "low": low, "close": mid, "ts": hour * 3_600_000}
            self._last_hour = hour
            return True

        # Update forming bar
        if self._current is not None:
            self._current["high"] = max(self._current["high"], high)
            self._current["low"] = min(self._current["low"], low)
            self._current["close"] = mid

        return False

    def _rebuild_arrays(self) -> None:
        """Rebuild flat arrays from bars deque."""
        self.closes = [b["close"] for b in self._bars]
        self.highs = [b["high"] for b in self._bars]
        self.lows = [b["low"] for b in self._bars]

    def ready(self) -> bool:
        return len(self._bars) >= MIN_HISTORY


# --- Strategy ---

class AutoresearchStrategy(BaseStrategy):
    """9-signal ensemble with SDO-adaptive ATR trailing stops.

    Live-adapted: hourly candle aggregation, USD-based position sizing.
    """

    def __init__(
        self,
        strategy_id: str = "autoresearch",
        tvl: float = 500.0,
        leverage: float = 3.0,
        coin_weight: float = 0.25,
        size: float = 0.0,  # ignored, kept for backward compat
    ):
        super().__init__(strategy_id=strategy_id)
        self.tvl = tvl
        self.leverage = leverage
        self.coin_weight = coin_weight

        self.candles = CandleAggregator(max_bars=500)
        self._bootstrapped = False
        self._coin: str = ""

        self.direction: int = 0       # 1=long, -1=short, 0=flat
        self.entry_price: float = 0.0
        self.peak_price: float = 0.0
        self.atr_at_entry: float = 0.0
        self.bar_count: int = 0
        self.exit_bar: int = -999

    def _compute_size(self, mid_price: float) -> float:
        """Compute order size in base units from USD-based sizing."""
        target_notional = self.tvl * BASE_POSITION_PCT * self.coin_weight * self.leverage
        if target_notional < HL_MIN_NOTIONAL:
            return 0.0
        size_base = target_notional / mid_price
        decimals = SZ_DECIMALS.get(self._coin, 4)
        return round(size_base, decimals)

    def on_tick(
        self,
        snapshot: MarketSnapshot,
        context: Optional[StrategyContext] = None,
    ) -> List[StrategyDecision]:
        mid = snapshot.mid_price
        if mid <= 0:
            return []

        high = snapshot.ask if snapshot.ask > 0 else mid
        low = snapshot.bid if snapshot.bid > 0 else mid
        ctx = context or StrategyContext()

        # Extract coin name on first tick
        if not self._coin:
            self._coin = snapshot.instrument.replace("-PERP", "").replace("-perp", "")

        # Bootstrap historical candles on first tick
        if not self._bootstrapped:
            self._bootstrapped = True
            self.candles.bootstrap(self._coin)

        # Feed tick to candle aggregator
        ts_ms = snapshot.timestamp_ms if hasattr(snapshot, "timestamp_ms") and snapshot.timestamp_ms else int(_time.time() * 1000)
        bar_closed = self.candles.update(mid, high, low, ts_ms)

        # Sync direction with actual position
        if ctx.position_qty > 0:
            self.direction = 1
        elif ctx.position_qty < 0:
            self.direction = -1
        elif self.direction != 0 and ctx.position_qty == 0:
            self.direction = 0

        if not self.candles.ready():
            return []

        closes = self.candles.closes
        highs = self.candles.highs
        lows = self.candles.lows

        # --- Exit checks run every tick (trailing stop, RSI exit) ---
        if self.direction != 0:
            exit_orders = self._check_exits(closes, highs, lows, mid, snapshot, ctx)
            if exit_orders:
                return exit_orders

        # --- Entry signals only on bar close ---
        if not bar_closed:
            return []

        self.bar_count += 1
        return self._check_entries(closes, highs, lows, mid, snapshot, ctx)

    def _check_entries(
        self,
        closes: list, highs: list, lows: list,
        mid: float, snapshot: MarketSnapshot, ctx: StrategyContext,
    ) -> List[StrategyDecision]:
        """Generate entry signals (called on bar close only)."""
        # --- Dynamic threshold ---
        vol = _calc_vol(closes, VOL_LOOKBACK)
        vol_ratio = vol / TARGET_VOL
        dyn_threshold = BASE_THRESHOLD * (0.3 + vol_ratio * 0.7)
        dyn_threshold = max(0.005, min(0.025, dyn_threshold))

        # --- 9 Signals ---
        # 1. Momentum 12h
        ret_short = math.log(closes[-1] / closes[-MED_WINDOW]) if closes[-MED_WINDOW] > 0 else 0
        mom_bull = ret_short > dyn_threshold
        mom_bear = ret_short < -dyn_threshold

        # 2. Very-short momentum 6h
        ret_vshort = math.log(closes[-1] / closes[-SHORT_WINDOW]) if closes[-SHORT_WINDOW] > 0 else 0
        vshort_bull = ret_vshort > dyn_threshold * 0.7
        vshort_bear = ret_vshort < -dyn_threshold * 0.7

        # 3. EMA(5/23) crossover
        ema_seg = closes[-(EMA_SLOW + 10):]
        ema_f = _ema(ema_seg, EMA_FAST)
        ema_s = _ema(ema_seg, EMA_SLOW)
        ema_bull = ema_f[-1] > ema_s[-1]
        ema_bear = ema_f[-1] < ema_s[-1]

        # 4. RSI(4) for entry
        rsi = _calc_rsi(closes, RSI_ENTRY_PERIOD)
        rsi_bull = rsi > RSI_BULL
        rsi_bear = rsi < RSI_BEAR

        # 5. MACD(7/30/2)
        macd_hist = _calc_macd(closes)
        macd_bull = macd_hist > 0
        macd_bear = macd_hist < 0

        # 6. BB width compression
        bb_pctile = _calc_bb_width_pctile(closes, BB_PERIOD)
        bb_compressed = bb_pctile < 93

        # 7. RSI divergence (using RSI_PERIOD=8 for quality)
        _, has_bull_div, has_bear_div = _calc_rsi_divergence(closes, RSI_PERIOD, RSI_DIV_LOOKBACK)

        # 8. Donchian 5-bar 70% breakout
        donch_seg = closes[-DONCH_PERIOD - 1:-1]
        if len(donch_seg) >= DONCH_PERIOD:
            donch_high = max(donch_seg)
            donch_low = min(donch_seg)
            donch_range = donch_high - donch_low
            donch_bull = closes[-1] >= donch_low + donch_range * DONCH_PCT
            donch_bear = closes[-1] <= donch_low + donch_range * (1 - DONCH_PCT)
        else:
            donch_bull = donch_bear = False

        # 9. 3-bar micro momentum
        micro_ret = math.log(closes[-1] / closes[-3]) if closes[-3] > 0 else 0
        micro_bull = micro_ret > 0
        micro_bear = micro_ret < 0

        bull_votes = sum([mom_bull, vshort_bull, ema_bull, rsi_bull, macd_bull,
                          bb_compressed, has_bull_div, donch_bull, micro_bull])
        bear_votes = sum([mom_bear, vshort_bear, ema_bear, rsi_bear, macd_bear,
                          bb_compressed, has_bear_div, donch_bear, micro_bear])

        bullish = bull_votes >= MIN_VOTES
        bearish = bear_votes >= MIN_VOTES
        in_cooldown = (self.bar_count - self.exit_bar) < COOLDOWN_BARS

        signal_meta = {
            "bull_votes": bull_votes,
            "bear_votes": bear_votes,
            "rsi": round(rsi, 1),
            "macd": round(macd_hist, 6),
            "bb_pctile": round(bb_pctile, 1),
            "threshold": round(dyn_threshold, 4),
        }

        orders: List[StrategyDecision] = []
        entry_size = self._compute_size(mid)
        if entry_size <= 0:
            return []

        if self.direction == 0:
            # --- Entry ---
            if not in_cooldown and bullish:
                orders.append(StrategyDecision(
                    action="place_order",
                    instrument=snapshot.instrument,
                    side="buy",
                    size=entry_size,
                    limit_price=round(snapshot.ask, 8),
                    order_type="Ioc",
                    meta={**signal_meta, "signal": "ensemble_long",
                          "notional": round(entry_size * mid, 2)},
                ))
                self.direction = 1
                self.entry_price = mid
                self.peak_price = mid
                atr = _calc_atr(highs, lows, closes, ATR_LOOKBACK)
                self.atr_at_entry = atr if atr else mid * 0.02

            elif not in_cooldown and bearish:
                orders.append(StrategyDecision(
                    action="place_order",
                    instrument=snapshot.instrument,
                    side="sell",
                    size=entry_size,
                    limit_price=round(snapshot.bid, 8),
                    order_type="Ioc",
                    meta={**signal_meta, "signal": "ensemble_short",
                          "notional": round(entry_size * mid, 2)},
                ))
                self.direction = -1
                self.entry_price = mid
                self.peak_price = mid
                atr = _calc_atr(highs, lows, closes, ATR_LOOKBACK)
                self.atr_at_entry = atr if atr else mid * 0.02

        elif bullish and self.direction == -1 and not in_cooldown:
            # Signal flip: close short + open long
            orders.extend(self._flip(snapshot, ctx, "buy", entry_size, signal_meta, mid, highs, lows, closes))
        elif bearish and self.direction == 1 and not in_cooldown:
            # Signal flip: close long + open short
            orders.extend(self._flip(snapshot, ctx, "sell", entry_size, signal_meta, mid, highs, lows, closes))

        return orders

    def _flip(
        self, snapshot: MarketSnapshot, ctx: StrategyContext,
        new_side: str, entry_size: float, signal_meta: dict,
        mid: float, highs: list, lows: list, closes: list,
    ) -> List[StrategyDecision]:
        """Close current position and enter opposite direction."""
        orders = []
        close_side = "sell" if self.direction == 1 else "buy"
        close_price = snapshot.bid if self.direction == 1 else snapshot.ask
        close_size = abs(ctx.position_qty) if ctx.position_qty != 0 else entry_size

        # Close
        orders.append(StrategyDecision(
            action="place_order",
            instrument=snapshot.instrument,
            side=close_side,
            size=close_size,
            limit_price=round(close_price, 8),
            order_type="Ioc",
            meta={**signal_meta, "signal": "signal_flip"},
        ))

        # Enter opposite
        flip_price = snapshot.ask if new_side == "buy" else snapshot.bid
        orders.append(StrategyDecision(
            action="place_order",
            instrument=snapshot.instrument,
            side=new_side,
            size=entry_size,
            limit_price=round(flip_price, 8),
            order_type="Ioc",
            meta={**signal_meta, "signal": "flip_entry",
                  "notional": round(entry_size * mid, 2)},
        ))

        self.direction = 1 if new_side == "buy" else -1
        self.entry_price = mid
        self.peak_price = mid
        atr = _calc_atr(highs, lows, closes, ATR_LOOKBACK)
        self.atr_at_entry = atr if atr else mid * 0.02
        self.exit_bar = -999  # no cooldown on flip

        return orders

    def _check_exits(
        self,
        closes: list, highs: list, lows: list,
        mid: float, snapshot: MarketSnapshot, ctx: StrategyContext,
    ) -> List[StrategyDecision]:
        """Check exit conditions (called every tick for responsiveness)."""
        atr = _calc_atr(highs, lows, closes, ATR_LOOKBACK) or self.atr_at_entry
        exit_signal = None

        # SDO-adaptive ATR trailing stop
        sdo = _calc_sdo(highs, lows, closes)
        atr_mult = ATR_STOP_MULT
        if self.direction == 1 and sdo > SDO_OVERBOUGHT:
            atr_mult = SDO_TIGHT_ATR_MULT
        elif self.direction == -1 and sdo < SDO_OVERSOLD:
            atr_mult = SDO_TIGHT_ATR_MULT

        if self.direction == 1:
            self.peak_price = max(self.peak_price, mid)
            stop = self.peak_price - atr_mult * atr
            if mid < stop:
                exit_signal = "atr_trailing_stop"
        else:
            self.peak_price = min(self.peak_price, mid)
            stop = self.peak_price + atr_mult * atr
            if mid > stop:
                exit_signal = "atr_trailing_stop"

        # RSI mean-reversion exit (use entry RSI period = 4)
        if not exit_signal:
            rsi = _calc_rsi(closes, RSI_ENTRY_PERIOD)
            if self.direction == 1 and rsi > RSI_OVERBOUGHT:
                exit_signal = "rsi_exit"
            elif self.direction == -1 and rsi < RSI_OVERSOLD:
                exit_signal = "rsi_exit"

        if not exit_signal:
            return []

        close_side = "sell" if self.direction == 1 else "buy"
        close_price = snapshot.bid if self.direction == 1 else snapshot.ask
        close_size = abs(ctx.position_qty) if ctx.position_qty != 0 else self._compute_size(mid)

        self.direction = 0
        self.exit_bar = self.bar_count

        return [StrategyDecision(
            action="place_order",
            instrument=snapshot.instrument,
            side=close_side,
            size=close_size,
            limit_price=round(close_price, 8),
            order_type="Ioc",
            meta={"signal": exit_signal, "sdo": round(sdo, 1)},
        )]
