#!/usr/bin/env python3
"""
env.py — loads a project-local .env file, if present, into os.environ.

Why this exists: ALPACA_KEY/ALPACA_SECRET need to be different for
this project than for fpga-tick-engine (two separate Alpaca paper
accounts), but both projects read the SAME variable names. Exporting
both in your shell profile (~/.bashrc etc.) means whichever line comes
last wins globally, for every terminal, in both projects — not what
you want. Keeping each project's credentials in ITS OWN .env file,
loaded only when that project runs, avoids the collision entirely.

Deliberately stdlib-only (no python-dotenv dependency) since the
format needed here is trivial: KEY=VALUE per line, # comments, blank
lines ignored, optional quotes stripped.

Real environment variables always win over .env — so `export
ALPACA_KEY=...` in your current shell still overrides a .env file if
you ever want a one-off override.
"""

from __future__ import annotations

import os


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)   # setdefault: a real
                                                # `export` still wins
