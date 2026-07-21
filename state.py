#!/usr/bin/env python3
"""
state.py — persist/restore the ladder's scorecard + weekly-baseline
bookkeeping across restarts (laptop sleep, crash, manual restart).

One JSON file, rewritten atomically (write to a temp file, then
os.replace — never a half-written file if the process dies mid-save)
after every signal. Signals are infrequent enough (a handful a day at
most, for a strategy like this) that this is not a performance
concern.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime

FIELDS = ("signals", "blocked", "trips", "wins", "pnl_e4", "fees_usd",
          "positions", "opens", "baseline_e4", "levels_bought")


def save(card, baseline_week: dict, path: str) -> None:
    """baseline_week: symbol -> ISO 'YYYY-Www' string for the week each
    symbol's CURRENT baseline was computed for. Kept alongside the
    scorecard state so a restart can tell whether this week's
    re-anchor already happened, without re-fetching bars it doesn't
    need to."""
    data = {f: getattr(card, f) for f in FIELDS}
    data["trip_log"] = [
        {**t, "close_t": t["close_t"].isoformat() if t["close_t"] else None}
        for t in card.trip_log]
    data["baseline_week"] = baseline_week
    data["saved_at"] = datetime.now().isoformat()

    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def load(card, path: str) -> dict:
    """Mutates `card` in place with any previously-saved state. Returns
    the saved baseline_week dict ({} if no state file exists yet)."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = json.load(f)
    for name in FIELDS:
        if name in data:
            setattr(card, name, data[name])
    card.trip_log = [
        {**t, "close_t": (datetime.fromisoformat(t["close_t"])
                          if t["close_t"] else None)}
        for t in data.get("trip_log", [])]
    return data.get("baseline_week", {})
