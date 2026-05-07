"""Live adapter for native 5m strategy — signals computed every 5m bar.

Unlike the 1h adapter which only passes history at hour boundaries,
this adapter builds the history DataFrame on EVERY 5m bar close,
because the native 5m strategy computes indicators on raw 5m bars.

Wraps autoagent-hl/strategy_5m.py via AUTORESEARCH_PATH env var.
"""
from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, List, Optional

from common.models import MarketSnapshot, StrategyDecision
from sdk.strategy_sdk.base import BaseStrategy, StrategyContext

if TYPE_CHECKING:
    import numpy as np
    import pandas as pd

_AR_PATH = os.environ.get(
    "AUTORESEARCH_PATH",
    os.path.expanduser("~/autoagent-hl"),
)

MS_PER_5M = 300_000
MAX_5M_CANDLES = 800  # 5m strategy needs 600+ bars for vol lookback


class Autoresearch5mLiveAdapter(BaseStrategy):
    """Native 5m strategy adapter — signals every 5 minutes.

    Args:
        strategy_id: Identifier for this strategy instance.
        instrument: HL instrument symbol (e.g. "ETH" or "ETH-PERP").
        equity: Account equity in USD for position sizing.
        tvl: Alias for equity (backward compat).
    """

    def __init__(
        self,
        strategy_id: str = "autoresearch_5m",
        instrument: str = "ETH",
        equity: float = 1_000,
        tvl: Optional[float] = None,
        **kwargs,
    ):
        super().__init__(strategy_id=strategy_id)

        import numpy as np  # noqa: F811
        import pandas as pd  # noqa: F811
        self._np = np
        self._pd = pd

        os.environ["BAR_INTERVAL"] = "5m"

        # Import native 5m strategy
        if _AR_PATH not in sys.path:
            sys.path.insert(0, _AR_PATH)
        from strategy_5m import Strategy
        from prepare import BarData, PortfolioState, Signal, LOOKBACK_BARS
        if _AR_PATH in sys.path:
            sys.path.remove(_AR_PATH)

        self._BarData = BarData
        self._PortfolioState = PortfolioState
        self._Signal = Signal
        self._LOOKBACK_BARS = LOOKBACK_BARS

        self.instrument = instrument.replace("-PERP", "")
        self.equity = tvl if tvl is not None else equity

        self._candles: list[dict] = []
        self._current_candle: Optional[dict] = None
        self._candle_slot: Optional[int] = None

        self._strategy = Strategy(symbols=[self.instrument])

        self._pending_target: Optional[float] = None
        self._pending_since_slot: int = 0  # 5m slot when order was sent (independent of bar_count)
        self._RECONCILE_BARS: int = 3

        self._bootstrap_history()

    def _bootstrap_history(self):
        """Pre-load 700 5m candles from HL."""
        import time as _time
        import logging
        log = logging.getLogger("autoresearch_5m")

        try:
            import requests
            now_ms = int(_time.time() * 1000)
            start_ms = now_ms - MAX_5M_CANDLES * MS_PER_5M

            all_candles = []
            current = start_ms
            chunk_ms = 24 * 3_600_000

            while current < now_ms:
                for _retry in range(3):
                    try:
                        resp = requests.post(
                            "https://api.hyperliquid.xyz/info",
                            json={
                                "type": "candleSnapshot",
                                "req": {
                                    "coin": self.instrument,
                                    "interval": "5m",
                                    "startTime": current,
                                    "endTime": min(current + chunk_ms, now_ms),
                                },
                            },
                            timeout=15,
                        )
                        data = resp.json()
                        break
                    except Exception:
                        data = []
                        _time.sleep(1)
                if not data:
                    current += chunk_ms
                    continue
                for c in data:
                    all_candles.append({
                        "timestamp": int(c["t"]),
                        "open": float(c["o"]),
                        "high": float(c["h"]),
                        "low": float(c["l"]),
                        "close": float(c["c"]),
                        "volume": float(c["v"]),
                        "funding_rate": 0.0,
                    })
                current = int(data[-1]["t"]) + MS_PER_5M
                _time.sleep(0.15)

            seen = set()
            for c in sorted(all_candles, key=lambda x: x["timestamp"]):
                if c["timestamp"] not in seen:
                    seen.add(c["timestamp"])
                    self._candles.append(c)

            if self._candles and self._candles[-1]["timestamp"] // MS_PER_5M == now_ms // MS_PER_5M:
                self._candles.pop()
            if len(self._candles) > MAX_5M_CANDLES:
                self._candles = self._candles[-MAX_5M_CANDLES:]
            if self._candles:
                self._candle_slot = self._candles[-1]["timestamp"] // MS_PER_5M

            log.info(
                "Bootstrapped %d 5m candles for %s (native 5m mode)",
                len(self._candles), self.instrument,
            )
        except Exception as e:
            log.warning("Bootstrap failed for %s: %s", self.instrument, e)

    def on_tick(
        self,
        snapshot: MarketSnapshot,
        context: Optional[StrategyContext] = None,
    ) -> List[StrategyDecision]:
        mid = snapshot.mid_price
        if mid <= 0:
            return []

        ts_ms = snapshot.timestamp_ms
        slot = ts_ms // MS_PER_5M

        if slot != self._candle_slot:
            orders = []
            if self._current_candle is not None:
                self._candles.append(self._current_candle)
                if len(self._candles) > MAX_5M_CANDLES:
                    self._candles = self._candles[-MAX_5M_CANDLES:]
                orders = self._fire_on_bar(snapshot, context)

            self._candle_slot = slot
            self._current_candle = {
                "timestamp": slot * MS_PER_5M,
                "open": mid,
                "high": mid,
                "low": mid,
                "close": mid,
                "volume": 0.0,
                "funding_rate": snapshot.funding_rate,
            }
            return orders
        else:
            c = self._current_candle
            if c is not None:
                c["high"] = max(c["high"], mid)
                c["low"] = min(c["low"], mid)
                c["close"] = mid
            return []

    def _signed_notional(self, ctx: StrategyContext) -> float:
        """Derive signed notional from context.

        position_notional is abs(qty × price) — always positive.
        Use position_qty sign to recover direction.
        """
        if ctx.position_qty > 0:
            return ctx.position_notional    # long → positive
        elif ctx.position_qty < 0:
            return -ctx.position_notional   # short → negative
        return 0.0

    def _sync_strategy_state(self, ctx: StrategyContext, mid: float):
        sym = self.instrument
        strat = self._strategy
        signed = self._signed_notional(ctx)
        has_engine_pos = abs(signed) > 1.0
        has_strat_pos = strat.entry_prices.get(sym) is not None

        if has_engine_pos and not has_strat_pos:
            # Use engine's avg entry price if available (more accurate than mid)
            engine_entry = abs(ctx.position_notional / ctx.position_qty) if ctx.position_qty != 0 else mid
            strat.entry_prices[sym] = engine_entry
            strat.peak_prices[sym] = mid  # peak is current price (conservative)
            np = self._np
            if len(self._candles) > 204:  # 16 lookback × 12 bars + 12
                closes = np.array([c["close"] for c in self._candles])
                highs = np.array([c["high"] for c in self._candles])
                lows = np.array([c["low"] for c in self._candles])
                atr = strat._calc_atr(highs, lows, closes, 16)
            else:
                atr = None
            strat.atr_at_entry[sym] = atr if atr else mid * 0.02
            import logging
            logging.getLogger("autoresearch_5m").info(
                "Reconstructed state for %s: entry=%.2f, atr=%.4f, pos=$%.2f",
                sym, engine_entry, strat.atr_at_entry[sym], signed,
            )
        elif not has_engine_pos and has_strat_pos:
            strat.entry_prices.pop(sym, None)
            strat.peak_prices.pop(sym, None)
            strat.atr_at_entry.pop(sym, None)
            strat._current_stops.pop(sym, None)
            self._pending_target = None

    def _fire_on_bar(
        self,
        snapshot: MarketSnapshot,
        context: Optional[StrategyContext],
    ) -> List[StrategyDecision]:
        if len(self._candles) < 2:
            return []

        ctx = context or StrategyContext()
        self._sync_strategy_state(ctx, snapshot.mid_price)

        # Native 5m: build history DataFrame on EVERY bar
        df = self._pd.DataFrame(self._candles)

        last = self._candles[-1]
        bar = self._BarData(
            symbol=self.instrument,
            timestamp=last["timestamp"],
            open=last["open"],
            high=last["high"],
            low=last["low"],
            close=last["close"],
            volume=last["volume"],
            funding_rate=last["funding_rate"],
            history=df,
        )

        # Use SIGNED notional so strategy knows long vs short
        signed_notional = self._signed_notional(ctx)

        # Guard: if we sent an order and position hasn't reached target yet,
        # skip on_bar to prevent state corruption. Timeout after 6 slots (30min).
        # Uses candle_slot count (independent of bar_count which freezes when guard blocks).
        if self._pending_target is not None:
            current_slot = self._candle_slot or 0
            slots_waiting = current_slot - self._pending_since_slot
            target_reached = abs(signed_notional - self._pending_target) < 1.0
            timed_out = slots_waiting >= 6  # 6 × 5min = 30min

            if target_reached:
                self._pending_target = None
            elif not timed_out:
                return []
            else:
                import logging
                logging.getLogger("autoresearch_5m").warning(
                    "Pending order timed out for %s: target=%.1f, actual=%.1f, slots=%d",
                    self.instrument, self._pending_target, signed_notional, slots_waiting,
                )
                self._pending_target = None

        portfolio = self._PortfolioState(
            cash=self.equity - abs(signed_notional),
            positions={self.instrument: signed_notional} if abs(signed_notional) > 1.0 else {},
            entry_prices={},
            equity=self.equity + ctx.unrealized_pnl,
            timestamp=snapshot.timestamp_ms,
        )

        try:
            signals = self._strategy.on_bar({self.instrument: bar}, portfolio)
        except Exception as e:
            import logging
            logging.getLogger("autoresearch_5m").error(
                "Strategy on_bar failed for %s: %s", self.instrument, e,
            )
            return []

        # Debug: log strategy state when holding a position
        if abs(signed_notional) > 1.0:
            import logging
            _log = logging.getLogger("autoresearch_5m")
            entry = self._strategy.entry_prices.get(self.instrument)
            atr = self._strategy.atr_at_entry.get(self.instrument)
            peak = self._strategy.peak_prices.get(self.instrument)
            _log.info(
                "DBG %s: pos=$%.1f mid=%.2f entry=%.2f atr=%.2f peak=%.2f | signals=%d",
                self.instrument, signed_notional, last["close"],
                entry or 0, atr or 0, peak or 0, len(signals),
            )

        orders = []
        bar_count = self._strategy.bar_count
        for sig in signals:
            if sig.symbol != self.instrument:
                continue
            delta = sig.target_position - signed_notional
            if abs(delta) < 1.0:
                continue

            target_changed = (
                self._pending_target is None
                or abs(sig.target_position - self._pending_target) > 1.0
            )
            current_slot = self._candle_slot or 0
            reconcile_expired = current_slot - self._pending_since_slot >= self._RECONCILE_BARS

            if not target_changed and not reconcile_expired:
                continue

            notional = abs(delta)
            if notional < 11.0:
                delta = (delta / abs(delta)) * 11.0 if delta != 0 else 0
                sig = self._Signal(
                    symbol=sig.symbol,
                    target_position=signed_notional + delta,
                )

            side = "buy" if delta > 0 else "sell"
            size_base = abs(delta) / snapshot.mid_price if snapshot.mid_price > 0 else 0
            if size_base <= 0:
                continue

            # IOC for all orders — ALO gets rejected on thin books (LINK, SUI)
            # and falls back to Gtc taker anyway. IOC is honest about the cost.
            price = snapshot.ask if side == "buy" else snapshot.bid
            if price <= 0:
                price = snapshot.mid_price
            order_type = "Ioc"

            orders.append(StrategyDecision(
                action="place_order",
                instrument=snapshot.instrument,
                side=side,
                size=round(size_base, 6),
                limit_price=round(price, 2),
                order_type=order_type,
                meta={
                    "source": "autoresearch_5m_native",
                    "target_usd": sig.target_position,
                    "delta_usd": delta,
                },
            ))
            self._pending_target = sig.target_position
            self._pending_since_slot = self._candle_slot or 0

        return orders
