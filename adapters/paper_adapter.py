"""Paper trading proxy — real market data, simulated execution.

Wraps a real DirectHLProxy: delegates all read-only market data methods,
simulates order execution locally with instant fills.  Lets you run
strategies against live prices without placing real orders.

Usage:
    hl run <strategy> --paper
    hl apex run --paper
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Dict, List, Optional

from parent.hl_proxy import HLFill
from parent.store import JSONLStore

log = logging.getLogger("adapters.paper")

ZERO = Decimal("0")

# Default fee rates (Hyperliquid standard)
DEFAULT_TAKER_FEE = Decimal("0.00035")   # 3.5 bps
DEFAULT_MAKER_FEE = Decimal("0.0001")    # 1.0 bps


class PaperTradingProxy:
    """Paper trading proxy — real market data, simulated execution.

    Wraps a real DirectHLProxy.  Market data methods (get_snapshot,
    get_candles, get_all_mids, get_all_markets, get_account_state) are
    forwarded to the real exchange.  Execution methods (place_order,
    cancel_order, etc.) are simulated locally.

    Fill simulation: IOC orders fill at the current mid price; GTC/ALO
    orders fill at the limit price.  All fills are instant (no order
    book simulation).  Fees are applied at configurable rates.
    """

    def __init__(
        self,
        real_proxy,
        data_dir: str = "data/paper",
        taker_fee: Decimal = DEFAULT_TAKER_FEE,
        maker_fee: Decimal = DEFAULT_MAKER_FEE,
        paper_balance: Optional[float] = None,
    ):
        self._real = real_proxy
        self._taker_fee = taker_fee
        self._maker_fee = maker_fee
        self._trade_log = JSONLStore(path=f"{data_dir}/paper_trades.jsonl")

        # Paper state — use configured balance if exchange returns $0
        self._paper_balance_override = paper_balance
        self._initial_balance: Optional[float] = None
        self._paper_realized_pnl = ZERO
        self._paper_fees = ZERO

        # Trigger orders (local)
        self._trigger_orders: Dict[str, Dict] = {}
        self._next_trigger_oid = 9000

        # Leverage store (local, no exchange call)
        self._leverage: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Properties — delegated to real proxy (APEX uses these)
    # ------------------------------------------------------------------

    @property
    def _info(self):
        return self._real._info

    @property
    def _exchange(self):
        return self._real._exchange

    @property
    def _address(self):
        return self._real._address

    # ------------------------------------------------------------------
    # Market data — delegated to real exchange
    # ------------------------------------------------------------------

    def get_snapshot(self, instrument: str = "ETH-PERP"):
        snap = self._real.get_snapshot(instrument)
        self._check_triggers(instrument, snap.mid_price)
        return snap

    def get_candles(self, coin: str, interval: str, lookback_ms: int) -> list:
        return self._real.get_candles(coin, interval, lookback_ms)

    def get_all_markets(self) -> list:
        return self._real.get_all_markets()

    def get_all_mids(self) -> Dict[str, str]:
        return self._real.get_all_mids()

    def get_account_state(self) -> Dict:
        """Fetch real account state, overlay paper P&L on balance."""
        state = self._real.get_account_state()
        if not state:
            return state

        # Seed initial balance on first call
        if self._initial_balance is None:
            exchange_balance = state.get("account_value", 0.0)
            if exchange_balance < 1.0 and self._paper_balance_override:
                self._initial_balance = self._paper_balance_override
                log.info("[PAPER] Exchange balance $%.2f too low, using configured paper_balance: $%.2f",
                         exchange_balance, self._initial_balance)
            else:
                self._initial_balance = exchange_balance
                log.info("[PAPER] Seeded balance from exchange: $%.2f", self._initial_balance)

        # Overlay paper P&L on account value
        paper_pnl = float(self._paper_realized_pnl - self._paper_fees)
        state["account_value"] = self._initial_balance + paper_pnl
        state["_paper_pnl"] = paper_pnl
        state["_paper_fees"] = float(self._paper_fees)
        state["_paper_mode"] = True
        return state

    # ------------------------------------------------------------------
    # Execution — simulated locally
    # ------------------------------------------------------------------

    def place_order(
        self,
        instrument: str,
        side: str,
        size: float,
        price: float,
        tif: str = "Ioc",
        builder: Optional[dict] = None,
    ) -> Optional[HLFill]:
        """Simulate an order fill locally using real market prices."""
        # Determine fill price
        if tif == "Ioc":
            # Fill at current market mid (or use snapshot bid/ask for realism)
            try:
                snap = self._real.get_snapshot(instrument)
                if side.lower() == "buy" and snap.ask > 0:
                    fill_price = snap.ask
                elif side.lower() == "sell" and snap.bid > 0:
                    fill_price = snap.bid
                else:
                    fill_price = snap.mid_price if snap.mid_price > 0 else price
            except Exception:
                fill_price = price
        else:
            # GTC/ALO: fill at limit price
            fill_price = price

        if fill_price <= 0:
            log.warning("[PAPER] Cannot fill — zero price for %s", instrument)
            return None

        # Compute fee
        fee_rate = self._maker_fee if tif == "Alo" else self._taker_fee
        fee = Decimal(str(size)) * Decimal(str(fill_price)) * fee_rate
        self._paper_fees += fee

        ts_ms = int(time.time() * 1000)
        fill = HLFill(
            oid=f"paper-{ts_ms}",
            instrument=instrument,
            side=side.lower(),
            price=Decimal(str(fill_price)),
            quantity=Decimal(str(size)),
            timestamp_ms=ts_ms,
            fee=fee,
        )

        # Log to paper trade JSONL
        self._trade_log.append({
            "oid": fill.oid,
            "instrument": instrument,
            "side": side.lower(),
            "price": str(fill_price),
            "size": str(size),
            "fee": str(fee),
            "tif": tif,
            "timestamp_ms": ts_ms,
        })

        log.info("[PAPER] Filled [%s]: %s %s %s @ %s (fee: %s)",
                 tif, side, size, instrument, fill_price, fee)
        return fill

    def cancel_order(self, instrument: str, oid: str) -> bool:
        """No-op — paper orders fill instantly."""
        return True

    def get_open_orders(self, instrument: str = "") -> List[Dict]:
        """Always empty — paper orders fill instantly."""
        return []

    def set_leverage(self, leverage: int, coin: str = "ETH", is_cross: bool = True):
        """Store locally — no exchange call."""
        self._leverage[coin] = leverage
        log.info("[PAPER] Leverage set: %s = %dx", coin, leverage)

    # ------------------------------------------------------------------
    # Trigger orders — simulated locally
    # ------------------------------------------------------------------

    def place_trigger_order(
        self,
        instrument: str,
        side: str,
        size: float,
        trigger_price: float,
        builder: Optional[dict] = None,
    ) -> Optional[str]:
        """Store a trigger order locally. Checked on each get_snapshot()."""
        oid = str(self._next_trigger_oid)
        self._next_trigger_oid += 1
        self._trigger_orders[oid] = {
            "instrument": instrument,
            "side": side,
            "size": size,
            "trigger_price": trigger_price,
        }
        log.info("[PAPER] Trigger order placed: %s %s %s trigger@%s (oid=%s)",
                 side, size, instrument, trigger_price, oid)
        return oid

    def cancel_trigger_order(self, instrument: str, oid: str) -> bool:
        """Remove a trigger order from local store."""
        removed = self._trigger_orders.pop(oid, None)
        if removed:
            log.info("[PAPER] Trigger order cancelled: oid=%s", oid)
        return removed is not None

    def _check_triggers(self, instrument: str, current_price: float):
        """Check if any trigger orders should fire at current price."""
        if not self._trigger_orders or current_price <= 0:
            return

        fired = []
        for oid, order in self._trigger_orders.items():
            if order["instrument"] != instrument:
                continue
            tp = order["trigger_price"]
            # Sell triggers fire when price drops to/below trigger
            # Buy triggers fire when price rises to/above trigger
            if order["side"].lower() == "sell" and current_price <= tp:
                fired.append(oid)
            elif order["side"].lower() == "buy" and current_price >= tp:
                fired.append(oid)

        for oid in fired:
            order = self._trigger_orders.pop(oid)
            log.info("[PAPER] Trigger fired: %s %s %s @ %s (trigger=%s)",
                     order["side"], order["size"], order["instrument"],
                     current_price, order["trigger_price"])
            self.place_order(
                instrument=order["instrument"],
                side=order["side"],
                size=order["size"],
                price=current_price,
                tif="Ioc",
            )
