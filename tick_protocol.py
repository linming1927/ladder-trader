#!/usr/bin/env python3
"""
tick_protocol.py — minimal shared constants/helpers.

Trimmed from the fpga-tick-engine project's tick_protocol.py: this
standalone project has no wire format, no FPGA frames, and no UART —
it only needs the two "sides" a signal can be, and the fixed-point
price conversion (dollars x 10000) that the strategy math is written
against.
"""

from __future__ import annotations

SIDE_NEUTRAL, SIDE_BUY, SIDE_SELL = 0x00, 0x01, 0x02
SIDE_NAME = {SIDE_BUY: "BUY", SIDE_SELL: "SELL", SIDE_NEUTRAL: "NEUTRAL"}


def dollars(price_e4: int) -> float:
    """Fixed-point x10000 -> float dollars, for display only (never math)."""
    return price_e4 / 10_000.0


def to_e4(price: float) -> int:
    """Float dollars -> fixed-point x10000."""
    return int(round(price * 10_000))
