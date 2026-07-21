#!/usr/bin/env python3
"""
runner.py — run the ladder strategy continuously against live market
data. No FPGA, no serial port — pure Python, meant to just be left
running (see README.md for keeping it alive across terminal closes,
reboots, etc.).

SCORE-ONLY: this places no real orders. It tracks what the ladder
would have done — buy/sell signals, position, weighted-average cost
basis, hypothetical P&L — the same way the strategy worked in the
original project. See ladder_strategy.py's module docstring for why
that's a deliberate choice (no stop-loss yet) and not an oversight.

Usage:
    export ALPACA_KEY=...
    export ALPACA_SECRET=...
    python3 runner.py --symbols SPY,QQQ

    # smoke-test the whole pipeline with synthetic ticks, no
    # credentials or market hours required:
    python3 runner.py --symbols SPY --source sim
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from ladder_strategy import (LadderScorecard, compute_weekly_baseline,
                             fetch_prior_week_bars)
from scorecard import comparison_report, monthly_breakdown_report
from tick_protocol import dollars, to_e4
import state as statefile

log = logging.getLogger("runner")

HOUSEKEEPING_INTERVAL_S = 30 * 60     # how often to check for a new
                                      # ISO week (and re-anchor if so)
                                      # and to checkpoint state


def current_week_label(now: datetime | None = None) -> str:
    """ISO 'YYYY-Www', UTC — matches the "today" reference
    fetch_prior_week_bars() uses internally, so the two never disagree
    about which week it currently is."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%G-W%V")


def parse_manual_baselines(spec: str | None) -> dict:
    """--baseline SPY:512.30,QQQ:441.10 -> {"SPY": 512.30, "QQQ": 441.10}"""
    out = {}
    if not spec:
        return out
    for pair in spec.split(","):
        sym, price = pair.split(":")
        out[sym.strip().upper()] = float(price)
    return out


class LadderRunner:
    def __init__(self, args):
        self.args = args
        self.symbols = [s.strip().upper() for s in args.symbols.split(",")]
        self.manual_baselines = parse_manual_baselines(args.baseline)
        self.card = LadderScorecard(
            f"Ladder {args.step*100:.0f}%/{args.levels}lvl",
            step_pct=args.step, max_levels=args.levels,
            qty_per_level=args.qty,
            max_notional_usd=(args.max_notional
                              if args.max_notional > 0 else None),
            live=False)
        self.baseline_week: dict[str, str] = statefile.load(
            self.card, args.state_file)
        self._stop = threading.Event()
        self._signal_log_lock = threading.Lock()
        self.feed = None
        self.live_trader = None      # set up in run() if --live

    def _save_state(self):
        with self._signal_log_lock:
            statefile.save(self.card, self.baseline_week, self.args.state_file)

    # ---- weekly baseline -------------------------------------------------
    def ensure_baselines(self, force: bool = False) -> None:
        wk = current_week_label()
        for sym in self.symbols:
            if not force and self.baseline_week.get(sym) == wk:
                continue
            if sym in self.manual_baselines and sym not in self.baseline_week:
                # manual override only applies to a symbol's FIRST
                # baseline (e.g. no market-data access yet); once a
                # week has been auto-computed, subsequent weeks
                # re-anchor automatically like every other symbol
                base = to_e4(self.manual_baselines[sym])
                self.card.set_baseline(sym, base)
                self.baseline_week[sym] = wk
                log.info("[baseline] %s manual override: $%.2f",
                        sym, dollars(base))
                continue
            try:
                bars = fetch_prior_week_bars(
                    sym, os.environ.get("ALPACA_KEY"),
                    os.environ.get("ALPACA_SECRET"), feed=self.args.feed)
                base = compute_weekly_baseline(bars, self.args.method)
                self.card.set_baseline(sym, base)
                self.baseline_week[sym] = wk
                log.info("[baseline] %s (%s, week %s): $%.2f",
                        sym, self.args.method, wk, dollars(base))
            except Exception as e:
                # don't crash the process over one bad API call — retry
                # at the next housekeeping tick. If this symbol has
                # NEVER gotten a baseline, on_tick() just stays inert
                # for it (returns None) until one succeeds.
                log.warning("[baseline] %s fetch failed (%s) — will "
                           "retry; ladder stays inert for %s until "
                           "then", sym, e, sym)

    def housekeeping_loop(self):
        while not self._stop.wait(HOUSEKEEPING_INTERVAL_S):
            self.ensure_baselines()
            statefile.save(self.card, self.baseline_week, self.args.state_file)

    # ---- tick handling -----------------------------------------------
    def on_trade(self, symbol: str, price_e4: int, size: int):
        ev = self.card.on_tick(symbol, price_e4)
        if ev is None:
            return
        side = "BUY" if ev["side"] == 1 else "SELL"

        if self.live_trader is not None:
            # async: order runs in a background thread, the scorecard
            # (and state.json) only updates once a real fill lands —
            # see live_trader.py. `outcome` here is just what's known
            # synchronously: submitted / dropped / skipped.
            outcome = self.live_trader.on_signal(ev)
        else:
            with self._signal_log_lock:
                outcome = self.card.on_signal(ev)
                statefile.save(self.card, self.baseline_week,
                              self.args.state_file)

        log.info("[signal] %s %s @ $%.2f -> %s",
                symbol, side, dollars(price_e4), outcome)
        self._append_audit(symbol, side, price_e4, outcome)

    def _append_audit(self, symbol, side, price_e4, outcome):
        rec = {"t": datetime.now(timezone.utc).isoformat(), "symbol": symbol,
               "side": side, "price": dollars(price_e4), "outcome": outcome}
        try:
            d = os.path.dirname(os.path.abspath(self.args.audit_file)) or "."
            os.makedirs(d, exist_ok=True)
            with open(self.args.audit_file, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            log.exception("failed writing audit log")

    # ---- reporting -----------------------------------------------------
    def report_loop(self):
        while not self._stop.wait(self.args.report_every_s):
            log.info("\n%s", comparison_report({"ladder": self.card}))

    def final_report(self):
        log.info("\n%s", comparison_report({"ladder": self.card}))
        log.info("\n%s", monthly_breakdown_report({"ladder": self.card}))

    # ---- run -------------------------------------------------------------
    def run(self):
        if self.args.live:
            from broker import AlpacaBroker
            from live_trader import LiveLadderTrader
            broker = AlpacaBroker()      # hard-coded to the paper endpoint
            self.live_trader = LiveLadderTrader(self.card, broker,
                                                on_fill=self._save_state)
            cap_note = (f"${self.args.max_notional:,.0f}/symbol"
                       if self.args.max_notional > 0 else "no $ cap")
            log.warning(
                "*** --live is ON: real market orders will be placed on "
                "the Alpaca PAPER account ALPACA_KEY/ALPACA_SECRET point "
                "to. The ladder still has no stop-loss — exposure is "
                "bounded only by max_levels (%s) * qty_per_level (%s) = "
                "%s shares AND %s, per symbol. ***",
                self.args.levels, self.args.qty,
                self.args.levels * self.args.qty, cap_note)
            self.live_trader.sync_from_broker(self.symbols)

        self.ensure_baselines(force=False)
        statefile.save(self.card, self.baseline_week, self.args.state_file)

        threading.Thread(target=self.housekeeping_loop, daemon=True).start()
        threading.Thread(target=self.report_loop, daemon=True).start()

        def handle_stop(signum, frame):
            log.info("shutting down (signal %s)...", signum)
            self._stop.set()
            if self.feed is not None:
                self.feed.stop()

        signal.signal(signal.SIGINT, handle_stop)
        signal.signal(signal.SIGTERM, handle_stop)

        if self.args.source == "sim":
            self._run_sim()
        else:
            from feed import AlpacaTradeFeed
            self.feed = AlpacaTradeFeed(self.symbols, self.on_trade,
                                        feed=self.args.feed,
                                        relay_url=self.args.relay_url)
            self.feed.run()      # blocks until stop()

        statefile.save(self.card, self.baseline_week, self.args.state_file)
        self.final_report()

    def _run_sim(self):
        """Synthetic random-walk ticks — no credentials, no market
        hours required. For smoke-testing the runner/state/reporting
        pipeline only; not a second strategy."""
        log.info("[sim] running synthetic ticks — Ctrl-C to stop")
        price = {s: to_e4(random.uniform(50, 500)) for s in self.symbols}
        while not self._stop.is_set():
            for sym in self.symbols:
                step = to_e4(price[sym] / 10_000 * random.uniform(-0.004, 0.004))
                price[sym] = max(to_e4(0.01), price[sym] + step)
                self.on_trade(sym, price[sym], random.randint(1, 200))
            time.sleep(self.args.sim_interval_s)


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Run the weekly-anchored ladder strategy continuously "
                    "(score-only — no real orders placed).")
    ap.add_argument("--symbols", default="SPY,QQQ",
                    help="comma-separated symbols to track")
    ap.add_argument("--step", type=float, default=0.03,
                    help="ladder trigger spacing, e.g. 0.03 = 3%%")
    ap.add_argument("--levels", type=int, default=3,
                    help="max buy levels before the ladder is 'full'")
    ap.add_argument("--qty", type=int, default=1,
                    help="shares bought at EACH level")
    ap.add_argument("--max-notional", type=float, default=2000.0,
                    help="cap on total dollars committed to ONE symbol "
                        "while a position is open (sum of price*qty "
                        "across every level bought so far) — a level "
                        "that would push past this doesn't fire, even "
                        "if --levels allows more. <=0 disables this cap")
    ap.add_argument("--method", default="week_vwap",
                    choices=["friday_close", "week_avg_close", "week_vwap",
                            "week_midpoint"],
                    help="how to compute each symbol's weekly baseline")
    ap.add_argument("--baseline", default=None,
                    help="manual override for a symbol's FIRST baseline "
                        "only, e.g. SPY:512.30,QQQ:441.10 — every "
                        "following week re-anchors automatically")
    ap.add_argument("--source", choices=["alpaca", "sim"], default="alpaca",
                    help="alpaca: live trades (needs ALPACA_KEY/SECRET). "
                        "sim: synthetic random walk, for smoke-testing")
    ap.add_argument("--feed", choices=["iex", "sip"], default="iex",
                    help="Alpaca data feed tier (sip needs a paid plan). "
                        "Ignored when --relay-url is set — the relay's "
                        "own --feed choice controls this instead.")
    ap.add_argument("--relay-url", default=None,
                    help="connect to a local alpaca_relay.py instance "
                        "instead of Alpaca directly, e.g. "
                        "ws://localhost:8765 — use this when running "
                        "alongside another project that also wants a "
                        "live Alpaca connection at the same time (only "
                        "one direct connection is allowed per Alpaca "
                        "login). See alpaca_relay.py's module docstring.")
    ap.add_argument("--live", action="store_true",
                    help="place REAL market orders on the Alpaca PAPER "
                        "account ALPACA_KEY/ALPACA_SECRET point to, "
                        "instead of only scoring hypothetical fills. "
                        "The strategy still has no stop-loss — see "
                        "live_trader.py's module docstring. Hard-coded "
                        "to Alpaca's paper endpoint; cannot be pointed "
                        "at a live/real-money account.")
    ap.add_argument("--sim-interval-s", type=float, default=1.0,
                    help="--source sim: seconds between synthetic ticks")
    ap.add_argument("--state-file", default="state/ladder_state.json")
    ap.add_argument("--audit-file", default="logs/signals.jsonl")
    ap.add_argument("--log-file", default="logs/ladder.log")
    ap.add_argument("--report-every-s", type=float, default=3600,
                    help="how often to log a status report")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return ap


def setup_logging(args):
    d = os.path.dirname(os.path.abspath(args.log_file)) or "."
    os.makedirs(d, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                 logging.FileHandler(args.log_file)])


def main():
    from env import load_dotenv
    load_dotenv()   # loads ./.env if present — real `export`s still win

    args = build_argparser().parse_args()
    if args.live and args.source == "sim":
        sys.exit("--live cannot be combined with --source sim — sim "
                 "prices are a synthetic random walk unrelated to the "
                 "real market, so real orders driven by them would fill "
                 "at whatever the ACTUAL market price happens to be, "
                 "not the synthetic trigger price. Use --source alpaca "
                 "for --live.")
    setup_logging(args)
    LadderRunner(args).run()


if __name__ == "__main__":
    main()
