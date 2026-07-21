#!/usr/bin/env python3
"""
live_trader.py — turns ladder signals into real (paper-account) orders.

This is the opt-in --live path. By default runner.py stays score-only,
exactly as ladder_strategy.py's own module docstring assumes. Turning
this on means every BUY/SELL signal the ladder generates becomes an
actual market order sent to whichever Alpaca account
ALPACA_KEY/ALPACA_SECRET point to.

*** This does NOT add a stop-loss. The ladder's own no-stop-loss,
*** unbounded-averaging-down design (see ladder_strategy.py's module
*** docstring) is unchanged by any of this — --live just means those
*** signals now move real (paper) shares instead of only updating a
*** scorecard. max_levels * qty_per_level is still your only cap on
*** exposure.

Safety/correctness properties this module adds — these are
requirements for placing real orders reliably, not strategy changes:
  * one order in flight per symbol at a time. A signal that fires
    while a previous order for that symbol hasn't resolved yet is
    logged and DROPPED, never queued or double-submitted.
  * a signal is skipped (not queued for later) while the market is
    closed.
  * the scorecard's cost basis is updated from the order's REAL fill
    price, not the triggering tick's price.
  * on startup, sync_from_broker() compares the broker's reported
    position against locally persisted state and logs any mismatch
    loudly rather than silently trusting either side — it does not
    try to guess/repair levels_bought, since a raw share count doesn't
    uniquely map back to a level count if qty_per_level ever changed
    between runs.
  * a SELL always sells the broker's ACTUAL current position size at
    the moment of the signal, never the locally-tracked qty — so a
    drift (crash before a save, a manual trade on the same account,
    etc.) can never cause an attempt to sell more than is actually
    held.
"""

from __future__ import annotations

import logging
import threading

from broker import AlpacaBroker, BrokerError
from tick_protocol import SIDE_BUY, dollars, to_e4

log = logging.getLogger("live_trader")


class LiveLadderTrader:
    def __init__(self, card, broker: AlpacaBroker, on_fill=None):
        self.card = card
        self.broker = broker
        self.on_fill = on_fill      # callback, called after each real fill
                                    # is folded into the scorecard — use it
                                    # to persist state (see runner.py)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, symbol: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(symbol, threading.Lock())

    def sync_from_broker(self, symbols: list[str]) -> None:
        """Call once at startup, before the feed connects. Logs a
        loud warning on any mismatch — does not auto-repair anything."""
        for sym in symbols:
            try:
                pos = self.broker.get_position(sym)
            except BrokerError as e:
                log.error("[live] couldn't fetch %s position at "
                         "startup: %s", sym, e)
                continue
            broker_qty = int(float(pos["qty"])) if pos else 0
            local_qty = self.card.positions.get(sym, 0)
            if broker_qty != local_qty:
                log.warning(
                    "[live] %s: broker reports %s shares, local state "
                    "says %s. These should match if this account is "
                    "ONLY ever traded by this process — if you traded "
                    "manually or ran another bot against it, reconcile "
                    "before trusting either the P&L numbers or the "
                    "next sell's size.", sym, broker_qty, local_qty)
            else:
                log.info("[live] %s: broker position (%s) matches "
                        "local state", sym, broker_qty)

    def on_signal(self, ev: dict) -> str:
        """Same contract as LadderScorecard.on_signal(): takes the
        event dict from card.on_tick(), returns a short status string.
        The actual order runs in a background thread so it never
        blocks the tick feed — the string returned here only reflects
        what's known synchronously (submitted / dropped / skipped);
        the eventual fill (or rejection) is logged separately when it
        lands, and only THEN is it folded into the scorecard."""
        sym = ev["symbol"]
        lock = self._lock_for(sym)
        if not lock.acquire(blocking=False):
            msg = f"dropped: order already in flight for {sym}"
            log.warning("[live] %s", msg)
            return msg

        # From here on WE hold the lock. Either release it ourselves
        # below (the two synchronous "never even submitted" paths), or
        # hand ownership to the background thread, which releases it
        # in its own finally once the order resolves. Exactly one of
        # those two must happen on every path.
        try:
            market_open = self.broker.is_market_open()
        except BrokerError as e:
            lock.release()
            log.error("[live] clock check failed, skipping signal: %s", e)
            return f"skipped: clock check failed ({e})"

        if not market_open:
            lock.release()
            return "skipped: market closed"

        threading.Thread(target=self._execute, args=(ev, lock),
                         daemon=True).start()
        return "submitted"

    def _execute(self, ev: dict, lock: threading.Lock):
        sym = ev["symbol"]
        side = "buy" if ev["side"] == SIDE_BUY else "sell"
        try:
            if side == "buy":
                qty = self.card.qty_per_level
            else:
                # ALWAYS sell what the broker actually holds right now
                # — never the locally-tracked qty. The one guarantee
                # that matters most here: never try to sell more than
                # we actually have.
                pos = self.broker.get_position(sym)
                qty = int(float(pos["qty"])) if pos else 0
                if qty <= 0:
                    log.warning("[live] %s: sell signal but broker "
                               "shows no open position — nothing to "
                               "do", sym)
                    return
                local_qty = self.card.positions.get(sym, 0)
                if qty != local_qty:
                    log.warning(
                        "[live] %s: selling broker's real qty (%s), "
                        "which does not match local state (%s) — P&L "
                        "recorded for this trip may be off; check "
                        "sync_from_broker's earlier warning", sym,
                        qty, local_qty)

            order = self.broker.submit_market_order(sym, qty, side)
            filled = self.broker.wait_for_fill(order["id"])
            status = filled.get("status")

            if status != "filled":
                log.error("[live] %s %s order %s did NOT fill "
                         "(status=%s) — scorecard unchanged, ladder "
                         "will retry on the next qualifying tick",
                         sym, side, order["id"], status)
                return

            fill_price = to_e4(float(filled["filled_avg_price"]))
            fill_qty = int(float(filled["filled_qty"]))
            log.info("[live] %s %s FILLED: %s sh @ $%.2f", sym, side,
                    fill_qty, dollars(fill_price))
            if side == "buy" and fill_qty != qty:
                log.warning(
                    "[live] %s: requested %s shares but %s filled — "
                    "local bookkeeping still assumes the full "
                    "qty_per_level (%s); verify against the broker's "
                    "real position", sym, qty, fill_qty,
                    self.card.qty_per_level)

            outcome = self.card.on_signal(
                {"side": ev["side"], "price_e4": fill_price,
                 "symbol": sym, "strategy": "ladder"})
            log.info("[live] %s scorecard update: %s", sym, outcome)

            if self.on_fill:
                self.on_fill()
        except BrokerError as e:
            log.error("[live] %s %s order failed: %s", sym, side, e)
        finally:
            lock.release()
