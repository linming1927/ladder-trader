#!/usr/bin/env python3
"""
scorecard.py — base position/P&L bookkeeping + reporting.

Trimmed from fpga-tick-engine's compare.py, which scored multiple
strategies side by side. This project only runs one (the ladder), so
the parts specific to gating an "untraded" row through a cloned
RiskPolicy, and the ProfitGatedScorecard variant, were dropped —
LadderScorecard (in ladder_strategy.py) overrides on_signal() itself
and doesn't use this base class's version at runtime, but it inherits
the dataclass fields (positions, opens, pnl_e4, trips, wins, fees_usd,
...) and the reporting helpers below, unchanged.

comparison_report() and monthly_breakdown_report() both still work
with a single-entry {"ladder": card} dict — useful for periodic status
logging in a long-running process.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from costs import FeeSchedule
from tick_protocol import SIDE_BUY, SIDE_SELL


@dataclass
class StrategyScorecard:
    name: str
    qty: int = 1                    # ungated (policy=None) fill size only
    fees: FeeSchedule = field(default_factory=FeeSchedule)
    policy: object | None = None    # unused here; kept for field-order
                                    # compatibility with the original class
    live: bool = False              # always False in this project — score
                                    # only, never a real broker fill

    signals: int = 0
    blocked: int = 0
    block_reasons: Counter = field(default_factory=Counter)
    trips: int = 0                  # completed round trips (buy -> sell)
    wins: int | None = 0
    pnl_e4: int = 0                 # gross, fixed-point x10000
    fees_usd: float = 0.0
    positions: dict = field(default_factory=dict)   # symbol -> qty (hyp.)
    opens: dict = field(default_factory=dict)        # symbol -> entry price
    trip_log: list = field(default_factory=list)     # one entry per CLOSED
                                                      # trip, for the monthly
                                                      # breakdown report

    @property
    def open_e4(self) -> int | None:
        return next(iter(self.opens.values()), None)

    def on_signal(self, fr: dict, count: bool = True,
                 t: datetime | None = None) -> str:
        """Base single-lot implementation. LadderScorecard overrides this
        entirely (it needs a weighted-average cost basis across multiple
        buy levels) — this version is here for completeness / reuse by
        any other simple strategy you might add later."""
        if count:
            self.signals += 1
        side, price = fr["side"], fr["price_e4"]
        sym = fr.get("symbol", "").strip()
        pos_qty = self.positions.get(sym, 0)

        if side == SIDE_BUY and pos_qty > 0:
            return "ignored: already open"
        if side == SIDE_SELL and pos_qty <= 0:
            return "ignored: flat"
        qty = self.qty if side == SIDE_BUY else pos_qty

        if side == SIDE_BUY:
            self.positions[sym] = qty
            self.opens[sym] = price
        elif side == SIDE_SELL:
            entry = self.opens.pop(sym, price)
            self.positions[sym] = 0
            if count:
                trip = (price - entry) * qty
                self.pnl_e4 += trip
                self.trips += 1
                trip_win = trip > 0
                if trip_win:
                    self.wins = (self.wins or 0) + 1
                fee = self.fees.sell_fees(qty, qty * price / 10_000.0)["total"]
                self.fees_usd += fee
                self.trip_log.append({
                    "close_t": t, "symbol": sym, "entry_e4": entry,
                    "exit_e4": price, "qty": qty, "pnl_e4": trip,
                    "fees_usd": fee, "win": trip_win})
        return "FILLED (scored)"

    @property
    def net_usd(self) -> float:
        return self.pnl_e4 / 10_000.0 - self.fees_usd

    def row(self) -> str:
        wr = (f"{100*self.wins/self.trips:.0f}%"
              if (self.wins is not None and self.trips) else "  —")
        open_n = sum(1 for v in self.positions.values() if v)
        open_s = f"{open_n} open" if open_n else "flat"
        blk = f"  ({self.blocked} gated)" if self.blocked else ""
        return (f"  {self.name:<24} {self.signals:>7} {self.trips:>6} "
                f"{wr:>5}  {self.pnl_e4/10_000:>+10.2f} {self.fees_usd:>7.2f} "
                f"{self.net_usd:>+10.2f}  {open_s}{blk}")


def comparison_report(cards: dict[str, StrategyScorecard]) -> str:
    lines = ["---- status (score-only: hypothetical fills, no real "
             "broker orders) ----",
             "  strategy                 signals  trips  win     gross $ "
             " fees $      net $  position"]
    lines += [c.row() for c in cards.values()]
    trips = [c.trips for c in cards.values()]
    if trips and max(trips) < 20:
        lines.append("  note: few round trips — treat as anecdote, "
                     "not evidence")
    return "\n".join(lines)


def monthly_breakdown_report(cards: dict[str, StrategyScorecard]) -> str:
    """Group each card's ALREADY-COMPLETED trips (trip_log) by the
    calendar month each one CLOSED in."""
    lines = ["---- monthly P&L breakdown ----"]
    any_trips = False
    for c in cards.values():
        if not c.trip_log:
            continue
        any_trips = True
        lines.append(f"\n  {c.name}:")
        lines.append(f"  {'month':<9} {'trips':>6} {'win':>5}  "
                     f"{'gross $':>10} {'fees $':>8} {'net $':>10}")
        by_month: dict[str, list] = {}
        for trip in c.trip_log:
            ym = (trip["close_t"].strftime("%Y-%m")
                 if trip["close_t"] is not None else "unknown")
            by_month.setdefault(ym, []).append(trip)
        for ym in sorted(by_month):
            month_trips = by_month[ym]
            n = len(month_trips)
            wins = sum(1 for tr in month_trips if tr["win"])
            gross = sum(tr["pnl_e4"] for tr in month_trips) / 10_000
            fees = sum(tr["fees_usd"] for tr in month_trips)
            wr = f"{100*wins/n:.0f}%" if n else "  —"
            lines.append(f"  {ym:<9} {n:>6} {wr:>5}  {gross:>+10.2f} "
                        f"{fees:>8.2f} {gross-fees:>+10.2f}")
        total_gross = sum(tr["pnl_e4"] for tr in c.trip_log) / 10_000
        total_fees = sum(tr["fees_usd"] for tr in c.trip_log)
        lines.append(f"  {'TOTAL':<9} {len(c.trip_log):>6} {'':>5}  "
                     f"{total_gross:>+10.2f} {total_fees:>8.2f} "
                     f"{total_gross-total_fees:>+10.2f}")
    if not any_trips:
        lines.append("\n  (no completed trips yet)")
    return "\n".join(lines)
