#!/usr/bin/env python3
"""
broker.py — thin wrapper over Alpaca's PAPER Trading REST API.

Hard-coded to https://paper-api.alpaca.markets — there is no
constructor argument, CLI flag, or env var anywhere in this project
that can point it at Alpaca's live (real-money) endpoint. If you ever
want that, it's a deliberate code change to this file, not a flag.

Handles just what live_trader.py needs: account/position lookups, a
market-hours check, submitting a market order, and polling it to a
terminal state. This is deliberately not a general Alpaca client —
reach for Alpaca's own alpaca-py SDK if you need more than this.

Uses ALPACA_KEY / ALPACA_SECRET — the same env vars feed.py and
ladder_strategy.py's baseline fetch already use. Point them at
whichever paper account you want THIS process to trade in (see
README.md's "running against a dedicated paper account" section).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

log = logging.getLogger("broker")

PAPER_BASE_URL = "https://paper-api.alpaca.markets"


class BrokerError(RuntimeError):
    pass


class AlpacaBroker:
    def __init__(self, key: str | None = None, secret: str | None = None):
        self.key = key or os.environ.get("ALPACA_KEY")
        self.secret = secret or os.environ.get("ALPACA_SECRET")
        if not (self.key and self.secret):
            sys.exit("set ALPACA_KEY and ALPACA_SECRET environment "
                     "variables (see README.md)")
        self.base_url = PAPER_BASE_URL     # not configurable — see module
                                           # docstring

    def _headers(self) -> dict:
        return {"APCA-API-KEY-ID": self.key,
                "APCA-API-SECRET-KEY": self.secret,
                "Content-Type": "application/json"}

    def _request(self, method: str, path: str, body: dict | None = None,
                timeout: float = 10) -> dict | list:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise BrokerError(f"{method} {path} -> HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise BrokerError(f"{method} {path} -> {e.reason}")

    # ---- read-only ---------------------------------------------------
    def get_account(self) -> dict:
        return self._request("GET", "/v2/account")

    def get_clock(self) -> dict:
        return self._request("GET", "/v2/clock")

    def is_market_open(self) -> bool:
        return bool(self.get_clock().get("is_open"))

    def get_position(self, symbol: str) -> dict | None:
        """None if flat (Alpaca 404s a symbol with no open position —
        that's not an error here, just "no position")."""
        try:
            return self._request("GET", f"/v2/positions/{symbol}")
        except BrokerError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    # ---- orders --------------------------------------------------------
    def submit_market_order(self, symbol: str, qty: int, side: str) -> dict:
        """side: 'buy' or 'sell'. Day market order — the simplest thing
        that reliably fills in a liquid, paper-tradeable symbol; if
        slippage on real (paper) fills becomes a concern, a limit-order
        variant would be the next step, not a change to this method's
        contract."""
        assert side in ("buy", "sell"), side
        body = {"symbol": symbol, "qty": str(qty), "side": side,
                "type": "market", "time_in_force": "day"}
        return self._request("POST", "/v2/orders", body)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/orders/{order_id}")

    def cancel_order(self, order_id: str) -> None:
        self._request("DELETE", f"/v2/orders/{order_id}")

    def wait_for_fill(self, order_id: str, timeout_s: float = 30,
                      poll_s: float = 0.5) -> dict:
        """Poll until the order reaches a terminal state (filled,
        canceled, rejected, expired, done_for_day) or timeout_s
        elapses. Returns the last order snapshot either way — the
        caller decides what a non-"filled" terminal status (or a
        timeout that never reached one) means for the strategy."""
        deadline = time.monotonic() + timeout_s
        terminal = {"filled", "canceled", "expired", "rejected",
                   "done_for_day"}
        while True:
            order = self.get_order(order_id)
            if order.get("status") in terminal:
                return order
            if time.monotonic() >= deadline:
                log.warning("order %s still %s after %ss timeout",
                           order_id, order.get("status"), timeout_s)
                return order
            time.sleep(poll_s)
