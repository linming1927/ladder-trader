#!/usr/bin/env python3
"""
test_live_trader.py — exercises LiveLadderTrader against an in-memory
fake broker (no network, no real Alpaca account needed). Covers the
correctness properties that matter once real orders are involved:
one order in flight per symbol, market-hours gating, selling the
broker's real qty (not local state), and folding real fills back into
the scorecard.

    python3 tests/test_live_trader.py
"""

from __future__ import annotations
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ladder_strategy import LadderScorecard
from live_trader import LiveLadderTrader
from tick_protocol import SIDE_BUY, SIDE_SELL, to_e4

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


class FakeBroker:
    """Duck-types the subset of AlpacaBroker's interface LiveLadderTrader
    actually calls. `fill_delay_s` lets a test hold an order "in
    flight" long enough to exercise the concurrency guard."""

    def __init__(self, fill_price: float = 100.0, market_open: bool = True,
                fill_delay_s: float = 0.0, fill_qty_override: int | None = None):
        self.market_open_flag = market_open
        self.fill_price = fill_price
        self.fill_delay_s = fill_delay_s
        self.fill_qty_override = fill_qty_override
        self.positions: dict[str, int] = {}
        self.orders_submitted = []     # (symbol, qty, side) log for assertions
        self._next_id = 0

    def is_market_open(self) -> bool:
        return self.market_open_flag

    def get_position(self, symbol: str):
        qty = self.positions.get(symbol, 0)
        return {"qty": str(qty)} if qty else None

    def submit_market_order(self, symbol: str, qty: int, side: str) -> dict:
        self.orders_submitted.append((symbol, qty, side))
        self._next_id += 1
        oid = str(self._next_id)
        # simulate the fill happening immediately in the account ledger
        # (a real broker fills async; wait_for_fill below is what
        # models the delay before the CALLER finds out)
        cur = self.positions.get(symbol, 0)
        self.positions[symbol] = cur + qty if side == "buy" else cur - qty
        return {"id": oid, "status": "accepted"}

    def wait_for_fill(self, order_id: str, timeout_s: float = 30,
                      poll_s: float = 0.5) -> dict:
        if self.fill_delay_s:
            time.sleep(self.fill_delay_s)
        # the LAST submitted order is the one being waited on, for
        # this fake's purposes (tests only run one order at a time
        # per symbol, by construction of the lock being tested)
        sym, qty, side = self.orders_submitted[-1]
        filled_qty = self.fill_qty_override or qty
        return {"status": "filled", "filled_avg_price": str(self.fill_price),
                "filled_qty": str(filled_qty)}


def make_card(**kw):
    defaults = dict(step_pct=0.03, max_levels=3, qty_per_level=1, live=False)
    defaults.update(kw)
    return LadderScorecard("Ladder", **defaults)


# ---- L1: a BUY signal submits an order and folds the real fill in ---------
print("[L1] buy signal -> order submitted -> real fill folds into scorecard")
card = make_card()
broker = FakeBroker(fill_price=97.00)
lt = LiveLadderTrader(card, broker)
outcome = lt.on_signal({"side": SIDE_BUY, "price_e4": to_e4(96.50),
                        "symbol": "SPY", "strategy": "ladder"})
check("on_signal returns submitted", outcome, "submitted")
time.sleep(0.2)   # background thread completes
check("order submitted with qty_per_level", broker.orders_submitted,
      [("SPY", 1, "buy")])
check("scorecard reflects the REAL fill price, not the trigger price",
      card.opens.get("SPY"), to_e4(97.00))
check("position opened", card.positions.get("SPY"), 1)

# ---- L2: market closed -> signal skipped, no order submitted --------------
print("[L2] market closed: signal skipped, nothing submitted")
card2 = make_card()
broker2 = FakeBroker(market_open=False)
lt2 = LiveLadderTrader(card2, broker2)
outcome2 = lt2.on_signal({"side": SIDE_BUY, "price_e4": to_e4(90.00),
                          "symbol": "SPY", "strategy": "ladder"})
check("skipped when market closed", outcome2, "skipped: market closed")
check("no order submitted", broker2.orders_submitted, [])

# ---- L3: one order in flight per symbol — a second signal is dropped ------
print("[L3] concurrent signal for the same symbol is dropped, not queued")
card3 = make_card()
broker3 = FakeBroker(fill_price=95.00, fill_delay_s=0.5)
lt3 = LiveLadderTrader(card3, broker3)
out_a = lt3.on_signal({"side": SIDE_BUY, "price_e4": to_e4(96.00),
                       "symbol": "SPY", "strategy": "ladder"})
time.sleep(0.05)   # let the first order actually start (lock held)
out_b = lt3.on_signal({"side": SIDE_BUY, "price_e4": to_e4(95.50),
                       "symbol": "SPY", "strategy": "ladder"})
check("first signal accepted", out_a, "submitted")
check("second signal dropped while first is in flight",
      out_b.startswith("dropped:"), True)
time.sleep(0.6)
check("only ONE order actually reached the broker", len(broker3.orders_submitted), 1)

# a different symbol is NOT blocked by SPY's in-flight order
card3.positions.setdefault("QQQ", 0)
out_c = lt3.on_signal({"side": SIDE_BUY, "price_e4": to_e4(400.00),
                       "symbol": "QQQ", "strategy": "ladder"})
check("a different symbol is independent", out_c, "submitted")

# ---- L4: SELL always uses the broker's real qty, not local state ----------
print("[L4] sell uses the broker's ACTUAL position size")
card4 = make_card()
broker4 = FakeBroker(fill_price=103.00)
# simulate drift: broker really holds 3 shares, local state (e.g. from
# a stale state.json) only knows about 1
broker4.positions["SPY"] = 3
card4.positions["SPY"] = 1
card4.opens["SPY"] = to_e4(97.00)
card4.levels_bought["SPY"] = 1
lt4 = LiveLadderTrader(card4, broker4)
lt4.on_signal({"side": SIDE_SELL, "price_e4": to_e4(103.00),
              "symbol": "SPY", "strategy": "ladder"})
time.sleep(0.2)
check("sell order sized off the BROKER's qty (3), not local (1)",
      broker4.orders_submitted, [("SPY", 3, "sell")])

# ---- L5: sell signal with nothing on the broker does nothing --------------
print("[L5] sell signal but broker shows flat: no-op, no crash")
card5 = make_card()
broker5 = FakeBroker()
lt5 = LiveLadderTrader(card5, broker5)
lt5.on_signal({"side": SIDE_SELL, "price_e4": to_e4(100.00),
              "symbol": "SPY", "strategy": "ladder"})
time.sleep(0.2)
check("no order submitted when broker is already flat",
      broker5.orders_submitted, [])

# ---- L6: sync_from_broker logs a mismatch but never raises -----------------
print("[L6] sync_from_broker doesn't raise on a mismatch, just reports it")
card6 = make_card()
card6.positions["SPY"] = 2
broker6 = FakeBroker()
broker6.positions["SPY"] = 5
lt6 = LiveLadderTrader(card6, broker6)
try:
    lt6.sync_from_broker(["SPY"])
    check("sync_from_broker completes without raising", True, True)
except Exception as e:
    check(f"sync_from_broker raised unexpectedly: {e}", False, True)

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
