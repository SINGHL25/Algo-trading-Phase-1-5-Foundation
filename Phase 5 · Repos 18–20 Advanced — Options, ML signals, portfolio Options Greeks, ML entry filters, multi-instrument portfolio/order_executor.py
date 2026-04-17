"""
core/order_executor.py
───────────────────────
Places, cancels, and monitors Kite orders for options strategies.

Rules enforced here:
  - Market hours check (9:15–15:30 IST)
  - Expiry day safety warnings
  - Order type: MARKET for entries (options spread is usually tight)
    or LIMIT at mid-price for better fills (configurable)
  - SL orders placed as SL-M immediately after entry
  - All orders logged to logs/orders.jsonl
"""

import os
import time
import json
import logging
from datetime import datetime, date, time as dtime
from pathlib import Path
import pytz
from kiteconnect import KiteConnect

from core.kite_client import get_kite

logger  = logging.getLogger(__name__)
IST     = pytz.timezone("Asia/Kolkata")
LOGFILE = Path("logs/orders.jsonl")

EXCHANGE       = os.getenv("EXCHANGE", "NFO")
PRODUCT_TYPE   = "MIS"    # MIS = intraday margin; NRML for overnight
ORDER_VARIETY  = "regular"

MARKET_OPEN    = dtime(9, 15)
MARKET_CLOSE   = dtime(15, 30)
FORCE_EXIT_TIME = dtime(15, 0)   # force-exit all before 3 PM


class OrderExecutor:
    def __init__(self):
        self.kite: KiteConnect = get_kite()
        LOGFILE.parent.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────────────────────────
    # Market hours
    # ──────────────────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        now = datetime.now(IST).time()
        return MARKET_OPEN <= now <= MARKET_CLOSE

    def is_past_force_exit(self) -> bool:
        return datetime.now(IST).time() >= FORCE_EXIT_TIME

    def is_expiry_today(self, expiry: date) -> bool:
        return expiry == date.today()

    # ──────────────────────────────────────────────────────────────────
    # Sell option (premium collection)
    # ──────────────────────────────────────────────────────────────────

    def sell_option(
        self,
        tradingsymbol: str,
        quantity: int,
        use_limit: bool = False,
        limit_price: float = 0.0,
    ) -> dict:
        """
        Sell (write) an option. Returns Kite order response dict.

        Args:
            tradingsymbol : e.g. 'NIFTY24JAN21500CE'
            quantity      : total units (lots × lot_size)
            use_limit     : use LIMIT order at limit_price; else MARKET
            limit_price   : only used when use_limit=True
        """
        if not self.is_market_open():
            raise RuntimeError("Market is closed. Cannot place order.")

        order_type = "LIMIT" if use_limit else "MARKET"
        params = {
            "variety":          ORDER_VARIETY,
            "exchange":         EXCHANGE,
            "tradingsymbol":    tradingsymbol,
            "transaction_type": self.kite.TRANSACTION_TYPE_SELL,
            "quantity":         quantity,
            "order_type":       order_type,
            "product":          PRODUCT_TYPE,
        }
        if use_limit:
            params["price"] = limit_price

        logger.info(f"Placing SELL order: {tradingsymbol} qty={quantity} type={order_type}")
        return self._place_order(params)

    # ──────────────────────────────────────────────────────────────────
    # Buy back (close / SL hit)
    # ──────────────────────────────────────────────────────────────────

    def buy_to_close(
        self,
        tradingsymbol: str,
        quantity: int,
        use_limit: bool = False,
        limit_price: float = 0.0,
    ) -> dict:
        """
        Buy back a previously sold option to close or cut loss.
        For SL exits: use_limit=False (MARKET to guarantee fill).
        """
        if not self.is_market_open():
            raise RuntimeError("Market is closed. Cannot place buy-back order.")

        order_type = "LIMIT" if use_limit else "MARKET"
        params = {
            "variety":          ORDER_VARIETY,
            "exchange":         EXCHANGE,
            "tradingsymbol":    tradingsymbol,
            "transaction_type": self.kite.TRANSACTION_TYPE_BUY,
            "quantity":         quantity,
            "order_type":       order_type,
            "product":          PRODUCT_TYPE,
        }
        if use_limit:
            params["price"] = limit_price

        logger.info(f"Placing BUY-TO-CLOSE: {tradingsymbol} qty={quantity} type={order_type}")
        return self._place_order(params)

    # ──────────────────────────────────────────────────────────────────
    # Place SL-M order (stop-loss market)
    # ──────────────────────────────────────────────────────────────────

    def place_sl_order(
        self,
        tradingsymbol: str,
        quantity: int,
        trigger_price: float,
    ) -> dict:
        """
        Place an SL-M order to auto-close when premium hits trigger_price.
        This is a BUY SL-M (to close a short option).
        """
        params = {
            "variety":          "regular",
            "exchange":         EXCHANGE,
            "tradingsymbol":    tradingsymbol,
            "transaction_type": self.kite.TRANSACTION_TYPE_BUY,
            "quantity":         quantity,
            "order_type":       "SL-M",
            "trigger_price":    round(trigger_price, 2),
            "product":          PRODUCT_TYPE,
        }
        logger.info(
            f"Placing SL-M order: {tradingsymbol} trigger={trigger_price:.2f} qty={quantity}"
        )
        return self._place_order(params)

    # ──────────────────────────────────────────────────────────────────
    # Order status
    # ──────────────────────────────────────────────────────────────────

    def get_order_status(self, order_id: str) -> dict:
        try:
            orders = self.kite.orders()
            for o in orders:
                if str(o["order_id"]) == str(order_id):
                    return o
        except Exception as e:
            logger.error(f"get_order_status failed for {order_id}: {e}")
        return {}

    def wait_for_fill(self, order_id: str, timeout: int = 30) -> float:
        """
        Poll until order is COMPLETE. Returns average fill price.
        Raises TimeoutError if not filled within timeout seconds.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.get_order_status(order_id)
            if status.get("status") == "COMPLETE":
                return float(status.get("average_price", 0.0))
            if status.get("status") in ("CANCELLED", "REJECTED"):
                raise RuntimeError(
                    f"Order {order_id} {status['status']}: "
                    f"{status.get('status_message', '')}"
                )
            time.sleep(1)
        raise TimeoutError(f"Order {order_id} not filled within {timeout}s")

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.kite.cancel_order(variety=ORDER_VARIETY, order_id=order_id)
            logger.info(f"Order {order_id} cancelled.")
            return True
        except Exception as e:
            logger.error(f"Cancel failed for {order_id}: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────

    def _place_order(self, params: dict) -> dict:
        try:
            order_id = self.kite.place_order(**params)
            result   = {"order_id": order_id, "status": "PLACED", **params}
            self._log_order(result)
            logger.info(f"Order placed: {order_id}")
            return result
        except Exception as e:
            logger.error(f"Order placement failed: {e} | Params: {params}")
            self._log_order({"error": str(e), "status": "FAILED", **params})
            raise

    def _log_order(self, record: dict):
        """Append order record to JSONL log file."""
        record["timestamp"] = datetime.now(IST).isoformat()
        try:
            with open(LOGFILE, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:
            logger.warning(f"Order log write failed: {e}")
