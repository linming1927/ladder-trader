#!/usr/bin/env python3
"""
alpaca_relay.py — one real Alpaca market-data connection, fanned out
to as many local processes as want it, each with its own dynamic
symbol subscription.

Alpaca allows exactly one concurrent market-data websocket connection
per account/subscription — confirmed in their own docs to hold even
on paid tiers like Algo Trader Plus, so upgrading doesn't raise it.
If more than one project wants live prices at the same time (e.g.
ladder-trader alongside fpga-tick-engine, even under two different
paper accounts — the limit is per login/subscription, not per
account), only one of them can hold that connection directly. This
relay holds it exactly once, upstream, and re-serves it locally.

Protocol: it speaks a close-enough subset of Alpaca's own websocket
protocol — the "connected"/"authenticated" control messages,
"subscribe", and raw trade ("t") messages — that either project's
EXISTING client code works against it completely unchanged. The only
thing that changes client-side is which URL it connects to (see
feed.py's --relay-url in ladder-trader, and bridge.py's --relay-url in
fpga-tick-engine). A client's auth message is accepted unconditionally
(the relay already authenticated upstream with ONE real key pair —
downstream clients don't need valid Alpaca credentials at all, though
in practice both projects still have their own for order placement,
which is separate from all of this).

This is READ-ONLY market data, nothing more. It has no path to order
placement — each project still submits its own orders directly to
Alpaca's trading REST API with its own account's keys, completely
independent of anything here.

Usage:
    export ALPACA_KEY=...      # either account's keys — market data
    export ALPACA_SECRET=...   # itself isn't account-specific
    python3 alpaca_relay.py --port 8765

Then point each project at ws://localhost:8765 instead of Alpaca's
real endpoint, and run them all as usual — subscriptions are unioned
dynamically as clients connect/subscribe, so it doesn't matter which
project asks for which symbols or in what order.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time

log = logging.getLogger("relay")


class UpstreamFeed:
    """The ONE real connection to Alpaca. websocket-client in its own
    thread (same reconnect-with-backoff shape as feed.py — see that
    module for why the backoff only resets on a REAL "authenticated"
    confirmation, not just on sending one). Every trade is handed to
    the asyncio loop via run_coroutine_threadsafe so the downstream
    broadcast side can stay fully async."""

    def __init__(self, loop: asyncio.AbstractEventLoop, on_trade,
                feed: str = "iex", key: str | None = None,
                secret: str | None = None):
        self.loop = loop
        self.on_trade = on_trade      # async def on_trade(symbol, msg_dict)
        self.feed = feed
        self.key = key or os.environ.get("ALPACA_KEY")
        self.secret = secret or os.environ.get("ALPACA_SECRET")
        if not (self.key and self.secret):
            sys.exit("set ALPACA_KEY and ALPACA_SECRET (either account's "
                     "keys work — see module docstring)")
        self._stop = threading.Event()
        self._ws = None
        self._symbols: set[str] = set()
        self._lock = threading.Lock()

    def add_symbols(self, symbols: set[str]) -> None:
        """Called from the asyncio side (downstream client subscribed
        to something new) — thread-safe, resubscribes upstream only if
        the union actually grew."""
        with self._lock:
            new = symbols - self._symbols
            if not new:
                return
            self._symbols |= symbols
            syms = list(self._symbols)
            ws = self._ws
        if ws is not None:
            try:
                ws.send(json.dumps({"action": "subscribe", "trades": syms}))
            except Exception:
                log.exception("upstream resubscribe failed")

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
            sys.exit("pip3 install websocket-client --break-system-packages")

        url = f"wss://stream.data.alpaca.markets/v2/{self.feed}"
        backoff = 1
        while not self._stop.is_set():
            authenticated = threading.Event()

            def on_open(ws):
                ws.send(json.dumps({"action": "auth", "key": self.key,
                                    "secret": self.secret}))
                with self._lock:
                    syms = list(self._symbols)
                if syms:
                    ws.send(json.dumps({"action": "subscribe", "trades": syms}))
                log.info("upstream: connected, auth sent (resubscribed %s)",
                        syms)

            def on_message(ws, message):
                for m in json.loads(message):
                    t = m.get("T")
                    if t == "t":
                        asyncio.run_coroutine_threadsafe(
                            self.on_trade(m.get("S"), m), self.loop)
                    elif t == "success" and m.get("msg") == "authenticated":
                        authenticated.set()
                        log.info("upstream: authenticated")
                    elif t == "error":
                        log.error("upstream error: %s", m)

            def on_error(ws, err):
                log.warning("upstream websocket error: %s", err)

            def on_close(ws, code, msg):
                log.warning("upstream closed (%s %s)", code, msg)

            self._ws = websocket.WebSocketApp(
                url, on_open=on_open, on_message=on_message,
                on_error=on_error, on_close=on_close)
            self._ws.run_forever()

            if self._stop.is_set():
                return
            log.warning("upstream disconnected — reconnecting in %ss", backoff)
            time.sleep(backoff)
            backoff = 1 if authenticated.is_set() else min(backoff * 2, 60)


class Relay:
    """The downstream (local) side: an asyncio websocket server that
    speaks just enough of Alpaca's protocol for existing client code
    to work against it unchanged."""

    def __init__(self, feed: str = "iex", port: int = 8765):
        self.port = port
        self.feed = feed
        self.clients: dict = {}     # websocket -> set(symbols)
        self._clients_lock = asyncio.Lock()
        self.upstream: UpstreamFeed | None = None

    async def broadcast_trade(self, symbol: str, msg: dict) -> None:
        async with self._clients_lock:
            targets = [ws for ws, syms in self.clients.items()
                      if symbol in syms]
        if not targets:
            return
        payload = json.dumps([msg])
        for ws in targets:
            try:
                await ws.send(payload)
            except Exception:
                pass   # a dead client is cleaned up by its own handler

    async def handle_client(self, ws):
        log.info("downstream client connected (%s)", ws.remote_address)
        async with self._clients_lock:
            self.clients[ws] = set()
        try:
            await ws.send(json.dumps([{"T": "success", "msg": "connected"}]))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                action = msg.get("action")
                if action == "auth":
                    await ws.send(json.dumps(
                        [{"T": "success", "msg": "authenticated"}]))
                elif action == "subscribe":
                    syms = {s.upper() for s in msg.get("trades", [])}
                    async with self._clients_lock:
                        self.clients[ws] |= syms
                        current = sorted(self.clients[ws])
                    if self.upstream is not None:
                        self.upstream.add_symbols(syms)
                    await ws.send(json.dumps(
                        [{"T": "subscription", "trades": current}]))
                    log.info("client subscribed to %s (now: %s)",
                            sorted(syms), current)
        except Exception:
            log.exception("downstream client error")
        finally:
            async with self._clients_lock:
                self.clients.pop(ws, None)
            log.info("downstream client disconnected")

    async def _on_upstream_trade(self, symbol, msg):
        await self.broadcast_trade(symbol, msg)

    async def serve(self):
        import websockets
        loop = asyncio.get_running_loop()
        self.upstream = UpstreamFeed(loop, self._on_upstream_trade,
                                     feed=self.feed)
        threading.Thread(target=self.upstream.run, daemon=True).start()

        async with websockets.serve(self.handle_client, "localhost",
                                    self.port):
            log.info("relay listening on ws://localhost:%s "
                    "(upstream feed: %s)", self.port, self.feed)
            await asyncio.Future()   # run forever


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(
        description="Relay ONE real Alpaca market-data connection out "
                    "to multiple local processes.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--feed", choices=["iex", "sip"], default="iex",
                    help="the upstream Alpaca feed tier this relay uses "
                        "for its one real connection")
    args = ap.parse_args()
    try:
        asyncio.run(Relay(feed=args.feed, port=args.port).serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
