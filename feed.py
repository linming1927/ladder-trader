#!/usr/bin/env python3
"""
feed.py — direct Alpaca trade websocket feed. No FPGA, no serial port.

In the original fpga-tick-engine project, ticks reached the ladder
strategy via a round trip through the physical board: bridge.py sent
each Alpaca trade to the FPGA over UART and only fed the strategy once
the hardware echoed it back. That's the piece this project needed to
drop to run standalone — here, a trade print goes straight from
Alpaca's websocket to your on_trade callback.

Needs ALPACA_KEY / ALPACA_SECRET env vars and the websocket-client
package (see requirements.txt).

Auto-reconnects with exponential backoff (capped at 60s) on any
disconnect or error, since this is meant to run unattended for days —
wifi blips, laptop sleep/wake, and Alpaca-side restarts should all be
things it recovers from on its own rather than going silently dark.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

from tick_protocol import to_e4

log = logging.getLogger("feed")


class AlpacaTradeFeed:
    """Subscribes to trade prints for `symbols` and calls
    on_trade(symbol, price_e4, size) for each one that arrives.

    run() is blocking — call it from a dedicated thread, or as your
    program's main loop. It only returns after stop() is called.
    """

    def __init__(self, symbols, on_trade, feed: str = "iex",
                key: str | None = None, secret: str | None = None):
        self.symbols = [s.strip().upper() for s in symbols]
        self.on_trade = on_trade
        self.feed = feed
        self.key = key or os.environ.get("ALPACA_KEY")
        self.secret = secret or os.environ.get("ALPACA_SECRET")
        if not (self.key and self.secret):
            sys.exit("set ALPACA_KEY and ALPACA_SECRET environment "
                     "variables (see README.md)")
        self._stop = threading.Event()
        self._ws = None

    def stop(self):
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def run(self):
        try:
            import websocket   # websocket-client
        except ImportError:
            sys.exit("this project needs:  pip3 install websocket-client "
                     "--break-system-packages")

        url = f"wss://stream.data.alpaca.markets/v2/{self.feed}"
        backoff = 1

        while not self._stop.is_set():
            authenticated = threading.Event()   # set only on a REAL
                                                # "authenticated" reply —
                                                # this is what the
                                                # backoff reset below
                                                # keys off of, so a
                                                # rejected handshake
                                                # (e.g. "connection
                                                # limit exceeded")
                                                # correctly backs off
                                                # instead of hammering
                                                # the server every ~1s

            def on_open(ws):
                ws.send(json.dumps({"action": "auth", "key": self.key,
                                    "secret": self.secret}))
                ws.send(json.dumps({"action": "subscribe",
                                    "trades": self.symbols}))
                log.info("sent auth + subscribe for: %s", self.symbols)

            def on_message(ws, message):
                for m in json.loads(message):
                    t = m.get("T")
                    if t == "t" and m.get("S") in self.symbols:
                        try:
                            self.on_trade(m["S"], to_e4(float(m["p"])),
                                         int(m.get("s", 0)))
                        except Exception:
                            log.exception("on_trade callback raised")
                    elif t == "success" and m.get("msg") == "authenticated":
                        authenticated.set()
                        log.info("authenticated — subscription confirmed")
                    elif t == "error":
                        log.error("alpaca stream error: %s", m)

            def on_error(ws, err):
                log.warning("websocket error: %s", err)

            def on_close(ws, code, msg):
                log.warning("websocket closed (%s %s)", code, msg)

            self._ws = websocket.WebSocketApp(
                url, on_open=on_open, on_message=on_message,
                on_error=on_error, on_close=on_close)
            self._ws.run_forever()          # blocks until closed / erred

            if self._stop.is_set():
                return
            log.warning("disconnected — reconnecting in %ss", backoff)
            time.sleep(backoff)
            # only reset backoff to fast-retry if we were GENUINELY
            # authenticated this attempt and later dropped (e.g. wifi
            # blip, laptop sleep/wake) — a handshake that never
            # actually succeeded (auth failure, "connection limit
            # exceeded", etc.) must back off properly instead of
            # hammering the server every ~1s, which can itself keep
            # colliding with a session that hasn't finished clearing
            # server-side yet
            backoff = 1 if authenticated.is_set() else min(backoff * 2, 60)
