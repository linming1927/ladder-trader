#!/usr/bin/env python3
"""
test_relay.py — exercises Relay's downstream side (the part that
matters for correctness: per-client symbol filtering and dynamic
subscription) against REAL local websocket connections, without ever
touching Alpaca. UpstreamFeed is never constructed — no credentials,
no network beyond localhost needed.

    python3 tests/test_relay.py
"""

from __future__ import annotations
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets
from alpaca_relay import Relay

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


async def recv_json(ws, timeout=2.0):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def main():
    relay = Relay(port=0)   # port unused directly — websockets.serve
                            # below picks an actual free port
    relay.upstream = None   # no real Alpaca connection in this test —
                            # add_symbols() no-ops when upstream is None

    async with websockets.serve(relay.handle_client, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://localhost:{port}"

        # ---- R1: connect handshake matches Alpaca's shape -----------------
        print("[R1] connect + auth handshake")
        async with websockets.connect(url) as c1:
            welcome = await recv_json(c1)
            check("welcome message", welcome, [{"T": "success", "msg": "connected"}])
            await c1.send(json.dumps({"action": "auth", "key": "x", "secret": "y"}))
            auth_reply = await recv_json(c1)
            check("auth accepted unconditionally", auth_reply,
                 [{"T": "success", "msg": "authenticated"}])

        # ---- R2: per-client symbol filtering -----------------------------
        print("[R2] two clients, different symbols, each gets only their own")
        async with websockets.connect(url) as spy_client, \
                  websockets.connect(url) as qqq_client:
            await recv_json(spy_client)   # connected
            await recv_json(qqq_client)
            await spy_client.send(json.dumps({"action": "subscribe",
                                              "trades": ["SPY"]}))
            await qqq_client.send(json.dumps({"action": "subscribe",
                                              "trades": ["QQQ"]}))
            sub_reply_spy = await recv_json(spy_client)
            sub_reply_qqq = await recv_json(qqq_client)
            check("SPY client's subscription confirmation",
                 sub_reply_spy, [{"T": "subscription", "trades": ["SPY"]}])
            check("QQQ client's subscription confirmation",
                 sub_reply_qqq, [{"T": "subscription", "trades": ["QQQ"]}])

            await relay.broadcast_trade(
                "SPY", {"T": "t", "S": "SPY", "p": 500.0, "s": 10})
            await relay.broadcast_trade(
                "QQQ", {"T": "t", "S": "QQQ", "p": 400.0, "s": 5})

            spy_msg = await recv_json(spy_client)
            qqq_msg = await recv_json(qqq_client)
            check("SPY client received the SPY trade",
                 spy_msg[0]["S"], "SPY")
            check("QQQ client received the QQQ trade",
                 qqq_msg[0]["S"], "QQQ")

            # neither client should have received the OTHER symbol —
            # confirm nothing else is sitting in the queue
            async def nothing_more(ws):
                try:
                    await asyncio.wait_for(ws.recv(), timeout=0.3)
                    return False
                except asyncio.TimeoutError:
                    return True
            check("SPY client got nothing else (no QQQ leakage)",
                 await nothing_more(spy_client), True)
            check("QQQ client got nothing else (no SPY leakage)",
                 await nothing_more(qqq_client), True)

        # ---- R3: dynamic subscription — add a symbol mid-session ----------
        print("[R3] a client can add a symbol dynamically after connecting")
        async with websockets.connect(url) as c3:
            await recv_json(c3)
            await c3.send(json.dumps({"action": "subscribe", "trades": ["AAPL"]}))
            await recv_json(c3)
            await relay.broadcast_trade("MSFT", {"T": "t", "S": "MSFT", "p": 1})
            got_early = None
            try:
                got_early = await asyncio.wait_for(c3.recv(), timeout=0.3)
            except asyncio.TimeoutError:
                pass
            check("not subscribed to MSFT yet: nothing received",
                 got_early, None)

            await c3.send(json.dumps({"action": "subscribe", "trades": ["MSFT"]}))
            sub2 = await recv_json(c3)
            check("subscription now covers both symbols",
                 sorted(sub2[0]["trades"]), ["AAPL", "MSFT"])
            await relay.broadcast_trade("MSFT", {"T": "t", "S": "MSFT", "p": 2})
            got = await recv_json(c3)
            check("MSFT trade received after dynamically subscribing",
                 got[0]["p"], 2)

        # ---- R4: a disconnected client is cleaned up, not leaked -----------
        print("[R4] disconnecting a client removes it from the broadcast set")
        c4 = await websockets.connect(url)
        await recv_json(c4)
        await c4.send(json.dumps({"action": "subscribe", "trades": ["TSLA"]}))
        await recv_json(c4)
        before = len(relay.clients)
        check("client registered before disconnect", before, 1)
        await c4.close()
        await asyncio.sleep(0.2)    # let the server-side handler finish
        after = len(relay.clients)
        check("client count dropped by exactly one after disconnect",
             before - after, 1)
        # broadcasting to the now-gone symbol should not raise
        try:
            await relay.broadcast_trade("TSLA", {"T": "t", "S": "TSLA", "p": 1})
            check("broadcast after disconnect doesn't raise", True, True)
        except Exception as e:
            check(f"broadcast after disconnect raised: {e}", False, True)

        # ---- R5: end-to-end with the REAL AlpacaTradeFeed client -----------
        print("[R5] ladder-trader's actual feed.py round-trips through the relay")
        from feed import AlpacaTradeFeed
        received = []
        client_feed = AlpacaTradeFeed(
            ["NVDA"], lambda sym, price_e4, size: received.append(
                (sym, price_e4, size)),
            key="fake", secret="fake", relay_url=url)
        t = __import__("threading").Thread(target=client_feed.run, daemon=True)
        t.start()
        await asyncio.sleep(0.5)   # let it connect + auth + subscribe

        async with relay._clients_lock:
            nvda_subscribed = any("NVDA" in syms for syms in
                                  relay.clients.values())
        check("feed.py's subscribe reached the relay", nvda_subscribed, True)

        await relay.broadcast_trade(
            "NVDA", {"T": "t", "S": "NVDA", "p": 123.45, "s": 7})
        await asyncio.sleep(0.3)
        check("feed.py's on_trade callback fired with the decoded trade",
             received, [("NVDA", 1234500, 7)])
        client_feed.stop()

    print(f"\n==============================================")
    print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
    print(f"==============================================")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
