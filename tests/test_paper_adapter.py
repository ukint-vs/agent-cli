"""Tests for PaperTradingProxy — real data passthrough + simulated execution."""
from __future__ import annotations

import os
import tempfile
from decimal import Decimal

import pytest

from adapters.paper_adapter import PaperTradingProxy
from cli.hl_adapter import DirectMockProxy
from parent.hl_proxy import HLFill


@pytest.fixture
def paper_dir(tmp_path):
    return str(tmp_path / "paper")


@pytest.fixture
def mock_proxy():
    """A DirectMockProxy acting as the 'real' exchange for tests."""
    return DirectMockProxy()


@pytest.fixture
def paper(mock_proxy, paper_dir):
    """PaperTradingProxy wrapping a mock as the real proxy."""
    return PaperTradingProxy(mock_proxy, data_dir=paper_dir)


# ------------------------------------------------------------------
# Market data passthrough
# ------------------------------------------------------------------

class TestMarketDataPassthrough:
    def test_get_snapshot_delegates(self, paper):
        snap = paper.get_snapshot("ETH-PERP")
        assert snap.instrument == "ETH-PERP"
        assert snap.mid_price > 0

    def test_get_candles_delegates(self, paper):
        candles = paper.get_candles("ETH", "1h", 3600000)
        assert isinstance(candles, list)

    def test_get_all_markets_delegates(self, paper):
        markets = paper.get_all_markets()
        assert isinstance(markets, list)

    def test_get_all_mids_delegates(self, paper):
        mids = paper.get_all_mids()
        assert isinstance(mids, dict)


# ------------------------------------------------------------------
# Fill simulation
# ------------------------------------------------------------------

class TestFillSimulation:
    def test_place_order_returns_fill(self, paper):
        fill = paper.place_order("ETH-PERP", "buy", 1.0, 2500.0, "Ioc")
        assert fill is not None
        assert isinstance(fill, HLFill)
        assert fill.oid.startswith("paper-")
        assert fill.instrument == "ETH-PERP"
        assert fill.side == "buy"
        assert fill.quantity == Decimal("1.0")

    def test_fill_price_ioc_uses_market(self, paper):
        """IOC fills should use the snapshot ask (for buys) or bid (for sells)."""
        fill = paper.place_order("ETH-PERP", "buy", 0.5, 2500.0, "Ioc")
        assert fill is not None
        # Price should come from mock snapshot, not the limit price
        assert float(fill.price) > 0

    def test_fill_price_gtc_uses_limit(self, paper):
        """GTC fills should use the limit price."""
        fill = paper.place_order("ETH-PERP", "sell", 0.5, 3000.0, "Gtc")
        assert fill is not None
        assert fill.price == Decimal("3000.0")

    def test_fill_price_alo_uses_limit(self, paper):
        """ALO fills should use the limit price."""
        fill = paper.place_order("ETH-PERP", "buy", 0.5, 2400.0, "Alo")
        assert fill is not None
        assert fill.price == Decimal("2400.0")

    def test_sell_fill(self, paper):
        fill = paper.place_order("ETH-PERP", "sell", 2.0, 2600.0, "Ioc")
        assert fill is not None
        assert fill.side == "sell"
        assert fill.quantity == Decimal("2.0")


# ------------------------------------------------------------------
# Fee calculation
# ------------------------------------------------------------------

class TestFeeCalculation:
    def test_taker_fee(self, paper):
        fill = paper.place_order("ETH-PERP", "buy", 1.0, 2500.0, "Ioc")
        assert fill is not None
        assert fill.fee > 0
        # Fee should be roughly size * price * 0.035%
        # (actual fill price comes from mock snapshot, so just check > 0)

    def test_maker_fee_lower_than_taker(self, mock_proxy, paper_dir):
        """ALO (maker) fee should be lower than IOC (taker) fee."""
        paper = PaperTradingProxy(mock_proxy, data_dir=paper_dir)
        # Use GTC to get deterministic limit-price fills
        fill_taker = paper.place_order("ETH-PERP", "buy", 1.0, 2500.0, "Gtc")
        taker_fee = fill_taker.fee

        paper2 = PaperTradingProxy(mock_proxy, data_dir=paper_dir + "2")
        fill_maker = paper2.place_order("ETH-PERP", "buy", 1.0, 2500.0, "Alo")
        maker_fee = fill_maker.fee

        # Same size and price, maker fee should be lower
        assert maker_fee < taker_fee

    def test_fees_accumulate(self, paper):
        paper.place_order("ETH-PERP", "buy", 1.0, 2500.0, "Gtc")
        paper.place_order("ETH-PERP", "sell", 1.0, 2500.0, "Gtc")
        assert paper._paper_fees > 0


# ------------------------------------------------------------------
# Account state
# ------------------------------------------------------------------

class TestAccountState:
    def test_seeds_initial_balance(self, paper):
        state = paper.get_account_state()
        assert paper._initial_balance is not None
        assert state.get("_paper_mode") is True

    def test_paper_pnl_in_state(self, paper):
        state = paper.get_account_state()
        assert "_paper_pnl" in state
        assert "_paper_fees" in state

    def test_balance_reflects_fees(self, paper):
        state1 = paper.get_account_state()
        initial_val = state1["account_value"]

        paper.place_order("ETH-PERP", "buy", 1.0, 2500.0, "Gtc")
        state2 = paper.get_account_state()
        # Balance should decrease by fees
        assert state2["account_value"] < initial_val


# ------------------------------------------------------------------
# Cancel / open orders (no-ops)
# ------------------------------------------------------------------

class TestCancelAndOpenOrders:
    def test_cancel_returns_true(self, paper):
        assert paper.cancel_order("ETH-PERP", "some-oid") is True

    def test_open_orders_empty(self, paper):
        assert paper.get_open_orders("ETH-PERP") == []


# ------------------------------------------------------------------
# Leverage (local store)
# ------------------------------------------------------------------

class TestLeverage:
    def test_set_leverage_stores_locally(self, paper):
        paper.set_leverage(10, "ETH")
        assert paper._leverage["ETH"] == 10


# ------------------------------------------------------------------
# Trigger orders
# ------------------------------------------------------------------

class TestTriggerOrders:
    def test_place_trigger_returns_oid(self, paper):
        oid = paper.place_trigger_order("ETH-PERP", "sell", 1.0, 2000.0)
        assert oid is not None
        assert oid in paper._trigger_orders

    def test_cancel_trigger(self, paper):
        oid = paper.place_trigger_order("ETH-PERP", "sell", 1.0, 2000.0)
        assert paper.cancel_trigger_order("ETH-PERP", oid) is True
        assert oid not in paper._trigger_orders

    def test_cancel_nonexistent_trigger(self, paper):
        assert paper.cancel_trigger_order("ETH-PERP", "99999") is False


# ------------------------------------------------------------------
# Trade log persistence
# ------------------------------------------------------------------

class TestTradeLog:
    def test_fills_logged_to_jsonl(self, paper, paper_dir):
        paper.place_order("ETH-PERP", "buy", 1.0, 2500.0, "Ioc")
        paper.place_order("ETH-PERP", "sell", 0.5, 2600.0, "Gtc")

        records = paper._trade_log.read_all()
        assert len(records) == 2
        assert records[0]["side"] == "buy"
        assert records[1]["side"] == "sell"
        assert records[0]["oid"].startswith("paper-")


# ------------------------------------------------------------------
# Properties
# ------------------------------------------------------------------

class TestPropertyDelegation:
    def test_info_property(self, paper, mock_proxy):
        """_info should delegate to the real proxy."""
        # DirectMockProxy wraps MockHLProxy which doesn't have _info,
        # but PaperTradingProxy._info accesses self._real._info.
        # For mock, _info doesn't exist — just verify the delegation path.
        # In production with real DirectHLProxy, this returns hl._info.
        pass  # Structural test — real integration needs live proxy
