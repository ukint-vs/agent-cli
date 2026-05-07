"""Live adapter — wraps auto-researchtrading strategy.py for agent-cli.

Bridges the interfaces:
  on_bar(bar_data: dict[sym->BarData], portfolio: PortfolioState) -> list[Signal]
  on_tick(snapshot: MarketSnapshot, context: StrategyContext) -> list[StrategyDecision]

The adapter:
1. Accumulates ticks into 5m OHLCV candles (aligned to 5-min boundaries)
2. At every 5m bar close, calls Strategy.on_bar() with history
3. The strategy internally aggregates 5m→1h for indicator computation
   (signals computed at hour boundaries, stops/TP checked every 5m)
4. Converts Signal (target_position USD) -> StrategyDecision (order delta)

5m execution gives 12x faster stop/TP checks vs 1h-only mode.
No manual re-porting needed — always runs the latest strategy.py.
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

# Path to auto-researchtrading repo (override via env var)
_AR_PATH = os.environ.get(
    "AUTORESEARCH_PATH",
    os.path.expanduser("~/Documents/projects/auto-researchtrading"),
)

MS_PER_5M = 300_000
MS_PER_HOUR = 3_600_000
BARS_5M_PER_HOUR = 12
MAX_5M_CANDLES = 700  # ~58 hours of 5m bars


class AutoresearchLiveAdapter(BaseStrategy):
    """Wraps auto-researchtrading's Strategy for live execution on Hyperliquid.

    Operates at 5m resolution: ticks are aggregated into 5m candles,
    on_bar() fires every 5 minutes. The strategy computes indicators on
    1h bars (aggregated internally from the 5m history) at hour boundaries,
    and checks stops/TP at every 5m bar using the cached signals.

    Args:
        strategy_id: Identifier for this strategy instance.
        instrument: HL instrument symbol (e.g. "ETH" or "ETH-PERP").
        equity: Account equity in USD for position sizing.
        tvl: Alias for equity (backward compat with legacy configs).
        leverage: Ignored (risk limits handle this at engine level).
        coin_weight: Ignored (single-instrument mode, weight=1.0).
    """

    def __init__(
        self,
        strategy_id: str = "autoresearch_live",
        instrument: str = "ETH",
        equity: float = 100_000,
        tvl: Optional[float] = None,
        leverage: Optional[float] = None,
        coin_weight: Optional[float] = None,
        **kwargs,
    ):
        super().__init__(strategy_id=strategy_id)

        # Lazy-import heavy deps
        import numpy as np  # noqa: F811
        import pandas as pd  # noqa: F811
        self._np = np
        self._pd = pd

        # Set BAR_INTERVAL=5m BEFORE importing strategy/prepare
        os.environ["BAR_INTERVAL"] = "5m"

        # Import strategy.py from auto-researchtrading
        if _AR_PATH not in sys.path:
            sys.path.insert(0, _AR_PATH)
        from strategy import Strategy
        from prepare import BarData, PortfolioState, Signal, LOOKBACK_BARS, BAR_MULTIPLIER
        if _AR_PATH in sys.path:
            sys.path.remove(_AR_PATH)
        self._BarData = BarData
        self._PortfolioState = PortfolioState
        self._Signal = Signal
        self._LOOKBACK_BARS = LOOKBACK_BARS  # 700 at 5m
        self._BAR_MULTIPLIER = BAR_MULTIPLIER  # 12

        # Normalize instrument
        self.instrument = instrument.replace("-PERP", "")
        self.equity = tvl if tvl is not None else equity

        # Rolling 5m candle buffer
        self._candles: list[dict] = []
        self._current_candle: Optional[dict] = None
        self._candle_slot: Optional[int] = None  # timestamp_ms // MS_PER_5M

        # The real strategy (single-symbol mode, BAR_MULTIPLIER=12)
        self._strategy = Strategy(symbols=[self.instrument])

        # Track last known position for delta calculation
        self._last_target: float = 0.0
        # Suppress duplicate orders: track what we already sent this signal cycle
        self._pending_target: Optional[float] = None  # target we're working toward
        self._pending_since: int = 0  # bar_count when we set the pending target
        self._RECONCILE_BARS: int = 3  # wait 3 bars (15 min) before re-sending

        # Bootstrap history from HL candleSnapshot API
        self._bootstrap_history()

    def _bootstrap_history(self):
        """Pre-load 700 5m candles from HL so strategy can trade immediately."""
        import time as _time
        import logging
        log = logging.getLogger("autoresearch")

        try:
            import requests
            now_ms = int(_time.time() * 1000)
            # 700 5m bars = ~58 hours
            start_ms = now_ms - MAX_5M_CANDLES * MS_PER_5M

            all_candles = []
            current = start_ms
            chunk_ms = 24 * MS_PER_HOUR  # 24h per request

            while current < now_ms:
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

            # Deduplicate and sort
            seen = set()
            for c in sorted(all_candles, key=lambda x: x["timestamp"]):
                if c["timestamp"] not in seen:
                    seen.add(c["timestamp"])
                    self._candles.append(c)

            # Trim to max buffer and skip last (incomplete)
            if self._candles and self._candles[-1]["timestamp"] // MS_PER_5M == now_ms // MS_PER_5M:
                self._candles.pop()  # drop current incomplete 5m bar
            if len(self._candles) > MAX_5M_CANDLES:
                self._candles = self._candles[-MAX_5M_CANDLES:]

            if self._candles:
                self._candle_slot = self._candles[-1]["timestamp"] // MS_PER_5M

            log.info(
                "Bootstrapped %d 5m candles for %s (ready to trade)",
                len(self._candles), self.instrument,
            )
        except Exception as e:
            log.warning(
                "Bootstrap failed for %s: %s (will accumulate from ticks)",
                self.instrument, e,
            )

    def on_tick(
        self,
        snapshot: MarketSnapshot,
        context: Optional[StrategyContext] = None,
    ) -> List[StrategyDecision]:
        mid = snapshot.mid_price
        if mid <= 0:
            return []

        ts_ms = snapshot.timestamp_ms
        slot = ts_ms // MS_PER_5M  # 5-minute slot

        if slot != self._candle_slot:
            # New 5m bar — close previous candle, fire strategy
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
            # Update current 5m candle
            c = self._current_candle
            if c is not None:
                c["high"] = max(c["high"], mid)
                c["low"] = min(c["low"], mid)
                c["close"] = mid
            return []

    def _signed_notional(self, ctx: "StrategyContext") -> float:
        """Derive signed notional from context.

        position_notional is abs(qty × price) — always positive.
        Use position_qty sign to recover direction.
        """
        if ctx.position_qty > 0:
            return ctx.position_notional
        elif ctx.position_qty < 0:
            return -ctx.position_notional
        return 0.0

    def _sync_strategy_state(self, ctx: "StrategyContext", mid: float):
        """Sync strategy's internal state with engine's real position."""
        sym = self.instrument
        strat = self._strategy
        engine_notional = self._signed_notional(ctx)
        has_engine_pos = abs(engine_notional) > 1.0
        has_strat_pos = strat.entry_prices.get(sym) is not None

        if has_engine_pos and not has_strat_pos:
            strat.entry_prices[sym] = mid
            strat.peak_prices[sym] = mid
            # Compute ATR from 1h aggregated data if available
            np = self._np
            if len(self._candles) > 16 * BARS_5M_PER_HOUR:
                closes = np.array([c["close"] for c in self._candles])
                highs = np.array([c["high"] for c in self._candles])
                lows = np.array([c["low"] for c in self._candles])
                atr = strat._calc_atr(highs, lows, closes, 16)
            else:
                atr = None
            strat.atr_at_entry[sym] = atr if atr else mid * 0.02
            strat._current_stops[sym] = (
                mid - 3.0 * strat.atr_at_entry[sym] if ctx.position_qty > 0
                else mid + 3.0 * strat.atr_at_entry[sym]
            )
            import logging
            logging.getLogger("autoresearch").info(
                "Reconstructed state for %s: entry=%.2f, pos=$%.2f",
                sym, mid, engine_notional,
            )

        elif not has_engine_pos and has_strat_pos:
            strat.entry_prices.pop(sym, None)
            strat.peak_prices.pop(sym, None)
            strat.atr_at_entry.pop(sym, None)
            strat._current_stops.pop(sym, None)
            self._pending_target = None  # position closed, reset pending

    def _fire_on_bar(
        self,
        snapshot: MarketSnapshot,
        context: Optional[StrategyContext],
    ) -> List[StrategyDecision]:
        if len(self._candles) < 2:
            return []

        ctx = context or StrategyContext()
        self._sync_strategy_state(ctx, snapshot.mid_price)

        last = self._candles[-1]
        is_hour_boundary = (last["timestamp"] % MS_PER_HOUR) == 0

        # Build history DataFrame only at hour boundaries (strategy uses it
        # for full indicator computation). Between hours, pass history=None
        # so the strategy uses cached signals + current price for stops/TP.
        if is_hour_boundary:
            df = self._pd.DataFrame(self._candles)
        else:
            df = None

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
        portfolio = self._PortfolioState(
            cash=self.equity - abs(signed_notional),
            positions={self.instrument: signed_notional} if abs(signed_notional) > 1.0 else {},
            entry_prices={},
            equity=self.equity + ctx.unrealized_pnl,
            timestamp=snapshot.timestamp_ms,
        )

        signals = self._strategy.on_bar(
            {self.instrument: bar},
            portfolio,
        )

        # Convert Signal -> StrategyDecision (with duplicate suppression)
        orders = []
        bar_count = self._strategy.bar_count
        for sig in signals:
            if sig.symbol != self.instrument:
                continue
            delta = sig.target_position - signed_notional
            if abs(delta) < 1.0:
                continue

            # Suppress duplicate orders: if we already sent an order toward this
            # target recently, don't re-send until reconciliation window expires.
            # This prevents pyramiding on every 5m bar when fills are slow.
            target_changed = (
                self._pending_target is None
                or abs(sig.target_position - self._pending_target) > 1.0
            )
            reconcile_expired = (
                bar_count - self._pending_since >= self._RECONCILE_BARS
            )

            if not target_changed and not reconcile_expired:
                # Same target, still within reconciliation window — skip
                continue

            # Enforce HL $10 minimum: bump small orders to $11 notional
            notional = abs(delta)
            if notional < 11.0:
                delta = (delta / abs(delta)) * 11.0 if delta != 0 else 0
                sig = self._Signal(
                    symbol=sig.symbol,
                    target_position=current_notional + delta,
                )

            side = "buy" if delta > 0 else "sell"
            size_base = abs(delta) / snapshot.mid_price if snapshot.mid_price > 0 else 0
            if size_base <= 0:
                continue
            price = snapshot.ask if side == "buy" else snapshot.bid
            if price <= 0:
                price = snapshot.mid_price
            orders.append(StrategyDecision(
                action="place_order",
                instrument=snapshot.instrument,
                side=side,
                size=round(size_base, 6),
                limit_price=round(price, 2),
                order_type="Ioc",
                meta={
                    "source": "autoresearch_live_5m",
                    "target_usd": sig.target_position,
                    "delta_usd": delta,
                    "hour_boundary": is_hour_boundary,
                    "reconcile": "new_target" if target_changed else "retry",
                },
            ))
            self._last_target = sig.target_position
            self._pending_target = sig.target_position
            self._pending_since = bar_count

        # Clear pending when position reaches target (filled)
        if self._pending_target is not None and abs(signed_notional - self._pending_target) < 1.0:
            self._pending_target = None

        return orders
