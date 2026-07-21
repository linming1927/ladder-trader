#!/usr/bin/env python3
"""
costs.py — regulatory sell-side fee model.

Trimmed from fpga-tick-engine's costs.py: keeps only FeeSchedule (used
by the scorecard to report a realistic net $, not just gross). The
original project's CostTracker and income-tax estimation helpers were
dropped since nothing in this ladder-only project calls them — if you
want tax estimates back, they're in the original repo's costs.py.

*** ESTIMATES ONLY. Not tax or financial advice.                    ***
*** Paper trading incurs no real fees — this models what a LIVE      ***
*** account with identical fills would pay in regulatory fees.       ***

Rates as verified July 2026 — they change; update the constants:
  * Commission: $0 (Alpaca).
  * SEC Section 31 fee: $20.60 per $1,000,000 of SALE proceeds.
  * FINRA Trading Activity Fee (TAF): $0.000195 per share SOLD, capped
    at $9.79 per trade.
  * Both apply to SELLS ONLY. Buys are free. Each fee rounds UP to the
    next cent per trade, matching broker pass-through practice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class FeeSchedule:
    sec_per_million: float = 20.60      # $ per $1M sale proceeds
    taf_per_share: float = 0.000195     # $ per share sold
    taf_cap: float = 9.79               # $ max TAF per trade
    cat_per_trade: float = 0.0          # optional flat CAT pass-through

    @staticmethod
    def _cent_up(x: float) -> float:
        return math.ceil(round(x * 100, 6)) / 100.0

    def sell_fees(self, qty: int, notional_usd: float) -> dict:
        """Regulatory fees for one SELL. Buys cost nothing."""
        sec = self._cent_up(notional_usd / 1_000_000 * self.sec_per_million)
        taf = self._cent_up(min(qty * self.taf_per_share, self.taf_cap))
        return {"sec": sec, "taf": taf, "cat": self.cat_per_trade,
                "total": sec + taf + self.cat_per_trade}
