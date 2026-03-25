"""Live adapter — wraps auto-researchtrading strategy.py for agent-cli.

Bridges the interfaces:
  on_bar(bar_data: dict[sym->BarData], portfolio: PortfolioState) -> list[Signal]
  on_tick(snapshot: MarketSnapshot, context: StrategyContext) -> list[StrategyDecision]

The adapter:
1. Accumulates ticks into a rolling 500-bar OHLCV DataFrame (1h candles)
2. On each new candle close, calls Strategy.on_bar() with the real interface
3. Converts Signal (target_position USD) -> StrategyDecision (order delta)

No manual re-porting needed — always runs the latest strategy.py.
"""
from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, List, Optional

from common.models import MarketSnapshot, StrategyDecision
from sdk.strategy_sdk.base import BaseStrategy, StrategyContext

# Heavy imports (numpy, pandas, strategy.py) are deferred to __init__
# so the module can be discovered without these deps installed.
if TYPE_CHECKING:
    import numpy as np
    import pandas as pd

# Path to auto-researchtrading repo (override via env var)
_AR_PATH = os.environ.get(
    "AUTORESEARCH_PATH",
    os.path.expanduser("~/Documents/projects/auto-researchtrading"),
)

MS_PER_HOUR = 3_600_000


class AutoresearchLiveAdapter(BaseStrategy):
    """Wraps auto-researchtrading's Strategy for live execution on Hyperliquid.

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

        # Lazy-import heavy deps (numpy, pandas, strategy.py)
        import numpy as np  # noqa: F811
        import pandas as pd  # noqa: F811
        self._np = np
        self._pd = pd

        # Import strategy.py from auto-researchtrading
        if _AR_PATH not in sys.path:
            sys.path.insert(0, _AR_PATH)
        from strategy import Strategy
        from prepare import BarData, PortfolioState, Signal, LOOKBACK_BARS
        if _AR_PATH in sys.path:
            sys.path.remove(_AR_PATH)
        self._BarData = BarData
        self._PortfolioState = PortfolioState
        self._Signal = Signal
        self._LOOKBACK_BARS = LOOKBACK_BARS

        # Normalize instrument: "ETH-PERP" -> "ETH", "SOL-PERP" -> "SOL"
        self.instrument = instrument.replace("-PERP", "")
        # tvl is legacy alias for equity
        self.equity = tvl if tvl is not None else equity

        # Rolling candle buffer (list of dicts → DataFrame for on_bar)
        self._candles: list[dict] = []
        self._current_candle: Optional[dict] = None
        self._candle_hour: Optional[int] = None

        # The real strategy (single-symbol mode)
        self._strategy = Strategy(symbols=[self.instrument])

        # Track last known position for delta calculation
        self._last_target: float = 0.0

        # Bootstrap history from HL candleSnapshot API
        self._bootstrap_history()

    def _bootstrap_history(self):
        """Pre-load 500 hours of 1h candles from HL so strategy can trade immediately."""
        import time as _time
        try:
            import requests
            now_ms = int(_time.time() * 1000)
            start_ms = now_ms - self._LOOKBACK_BARS * 3_600_000

            resp = requests.post(
                "https://api.hyperliquid.xyz/info",
                json={
                    "type": "candleSnapshot",
                    "req": {
                        "coin": self.instrument,
                        "interval": "1h",
                        "startTime": start_ms,
                        "endTime": now_ms,
                    },
                },
                timeout=10,
            )
            candles = resp.json()
            if not candles:
                return

            # Convert HL format to our candle dict format
            # HL: t(open_ms), o, c, h, l, v, n
            for c in candles[:-1]:  # skip last (incomplete current hour)
                self._candles.append({
                    "timestamp": int(c["t"]),
                    "open": float(c["o"]),
                    "high": float(c["h"]),
                    "low": float(c["l"]),
                    "close": float(c["c"]),
                    "volume": float(c["v"]),
                    "funding_rate": 0.0,  # not in candle data, strategy uses FUNDING_BOOST=0
                })

            # Trim to LOOKBACK_BARS
            if len(self._candles) > self._LOOKBACK_BARS:
                self._candles = self._candles[-self._LOOKBACK_BARS:]

            # Set candle hour to last complete candle so on_tick doesn't re-fire
            if self._candles:
                self._candle_hour = self._candles[-1]["timestamp"] // MS_PER_HOUR

            import logging
            logging.getLogger("autoresearch").info(
                "Bootstrapped %d candles for %s (ready to trade)",
                len(self._candles), self.instrument,
            )
        except Exception as e:
            import logging
            logging.getLogger("autoresearch").warning(
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

        hour = snapshot.timestamp_ms // MS_PER_HOUR

        if hour != self._candle_hour:
            # New hour — close previous candle, start new one
            orders = []
            if self._current_candle is not None:
                self._candles.append(self._current_candle)
                if len(self._candles) > self._LOOKBACK_BARS:
                    self._candles = self._candles[-self._LOOKBACK_BARS:]
                orders = self._fire_on_bar(snapshot, context)

            self._candle_hour = hour
            self._current_candle = {
                "timestamp": snapshot.timestamp_ms,
                "open": mid,
                "high": mid,
                "low": mid,
                "close": mid,
                "volume": 0.0,
                "funding_rate": snapshot.funding_rate,
            }
            return orders
        else:
            # Update current candle
            c = self._current_candle
            if c is not None:
                c["high"] = max(c["high"], mid)
                c["low"] = min(c["low"], mid)
                c["close"] = mid
            return []

    def _sync_strategy_state(self, ctx: "StrategyContext", mid: float):
        """Sync strategy's internal state with engine's real position.

        Fixes two problems:
        1. After engine restart, strategy has no memory of existing positions
        2. After partial fills, strategy's view diverges from engine's reality
        """
        sym = self.instrument
        strat = self._strategy
        engine_notional = ctx.position_notional  # signed USD from engine
        strat_notional = strat.entry_prices.get(sym) is not None

        has_engine_pos = abs(engine_notional) > 1.0
        has_strat_pos = strat_notional

        if has_engine_pos and not has_strat_pos:
            # Engine has position but strategy doesn't know — reconstruct state
            # Use mid as entry estimate (conservative — stop will be wide)
            strat.entry_prices[sym] = mid
            strat.peak_prices[sym] = mid
            atr = strat._calc_atr(
                self._pd.DataFrame(self._candles), 12
            ) if len(self._candles) > 13 else mid * 0.02
            strat.atr_at_entry[sym] = atr if atr else mid * 0.02
            strat._current_stops[sym] = (
                mid - 5.5 * strat.atr_at_entry[sym] if engine_notional > 0
                else mid + 5.5 * strat.atr_at_entry[sym]
            )
            import logging
            logging.getLogger("autoresearch").info(
                "Reconstructed state for %s: entry=%.2f, pos=$%.2f",
                sym, mid, engine_notional,
            )

        elif not has_engine_pos and has_strat_pos:
            # Engine has no position but strategy thinks it does — clear state
            strat.entry_prices.pop(sym, None)
            strat.peak_prices.pop(sym, None)
            strat.atr_at_entry.pop(sym, None)
            strat.pyramided.pop(sym, None)
            strat._current_stops.pop(sym, None)

    def _fire_on_bar(
        self,
        snapshot: MarketSnapshot,
        context: Optional[StrategyContext],
    ) -> List[StrategyDecision]:
        if len(self._candles) < 2:
            return []

        ctx = context or StrategyContext()

        # Sync strategy internal state with engine's real position
        self._sync_strategy_state(ctx, snapshot.mid_price)

        # Build history DataFrame matching prepare.py BarData.history format
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

        # Build PortfolioState from engine's real position (not strategy's guess)
        current_notional = ctx.position_notional
        portfolio = self._PortfolioState(
            cash=self.equity - abs(current_notional),
            positions={self.instrument: current_notional} if abs(current_notional) > 1.0 else {},
            entry_prices={},  # strategy tracks its own via _sync_strategy_state
            equity=self.equity + ctx.unrealized_pnl,
            timestamp=snapshot.timestamp_ms,
        )

        # Call the real strategy
        signals = self._strategy.on_bar(
            {self.instrument: bar},
            portfolio,
        )

        # Convert Signal -> StrategyDecision
        orders = []
        for sig in signals:
            if sig.symbol != self.instrument:
                continue
            delta = sig.target_position - current_notional
            if abs(delta) < 1.0:
                continue
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
                    "source": "autoresearch_live",
                    "target_usd": sig.target_position,
                    "delta_usd": delta,
                },
            ))
            self._last_target = sig.target_position
        return orders
