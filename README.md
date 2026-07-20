# ladder-trader

A standalone, pure-Python runner for the weekly-anchored mean-reversion
**ladder** strategy — split out of the `fpga-tick-engine` project. No
FPGA, no serial port, no hardware dependency at all: this talks
directly to Alpaca's market-data websocket and runs anywhere Python
does.

**Score-only.** This tracks what the ladder *would* have done — buy/sell
signals, position, weighted-average cost basis, hypothetical P&L — the
same way it worked in the original project. It never places a real
order or touches a broker. See the big warning below before you decide
whether to change that.

## The strategy, briefly

Once a week, a baseline price is set for each symbol (several methods
available — see `--method` below). From there:

```
level 1 buy   price <= baseline * (1 - step)
level 2 buy   price <= baseline * (1 - 2*step)
level 3 buy   price <= baseline * (1 - 3*step)      ... up to --levels
sell (all)    price >= baseline * (1 + step), while holding
```

Default `step` is 3%, default `--levels` is 3. The level index itself
provides hysteresis — no time-based cooldown needed, unlike a moving-
average crossover strategy.

### ⚠️ Read this before running it against real money decisions

This is a **grid / martingale-style averaging-down ladder**: exposure
*grows* as price falls further, which is backwards from "cut losses,
let winners run." It has **no stop-loss and no cap** beyond
`levels * qty`. It does fine in a range-bound week and can compound
badly in a sustained trend. That's not a bug — it's the current,
deliberate state of the strategy, carried over unchanged from the
original project. A few good weeks of score-only output prove nothing
about the one week a real trend runs through it.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export ALPACA_KEY=your_key
export ALPACA_SECRET=your_secret
```

Smoke-test the whole pipeline (state persistence, reporting, weekly
re-anchor bookkeeping) with synthetic ticks — no credentials or market
hours required:

```bash
python3 runner.py --symbols SPY --source sim --sim-interval-s 0.2
```

Run it against live data:

```bash
python3 runner.py --symbols SPY,QQQ
```

First run needs a baseline for each symbol. By default it fetches the
prior week's daily bars from Alpaca and computes one — that needs
`ALPACA_KEY`/`ALPACA_SECRET` regardless of `--source` (the ladder's
baseline calc always uses Alpaca's REST bars API; only the live tick
*stream* is skipped in `--source sim`). If you'd rather not hit the API
for a first run, override manually for that first week:

```bash
python3 runner.py --symbols SPY --baseline SPY:512.30
```

Every following week re-anchors automatically — the manual override
only ever applies to a symbol's very first baseline.

## Keeping it running

The process itself is resilient to network drops (`feed.py`
reconnects with backoff on its own) and to restarts (state is
persisted to disk after every signal, so stopping and starting again
picks up right where it left off — position, cost basis, and the
current week's baseline all survive). What it needs from *you* is
just: keep the process alive, and keep the laptop powered/awake during
market hours.

A few ways to do that, roughly in order of how much setup they need:

**Simplest — a terminal you leave open**, ideally inside `tmux` or
`screen` so closing the terminal window doesn't kill it:
```bash
tmux new -s ladder
./run.sh --symbols SPY,QQQ
# detach: Ctrl-B then D — reattach later with: tmux attach -t ladder
```
`run.sh` also restarts `runner.py` automatically if the process itself
ever crashes (uncaught exception, etc.) — Ctrl-C still stops it for
good.

**A user-level systemd service (Linux)**, so it comes back after a
reboot too:
```ini
# ~/.config/systemd/user/ladder-trader.service
[Unit]
Description=ladder-trader

[Service]
WorkingDirectory=%h/ladder-trader
ExecStart=%h/ladder-trader/.venv/bin/python3 runner.py --symbols SPY,QQQ
Environment=ALPACA_KEY=your_key
Environment=ALPACA_SECRET=your_secret
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```
```bash
systemctl --user enable --now ladder-trader
journalctl --user -u ladder-trader -f     # follow the logs
```
(Also enable lingering — `loginctl enable-linger $USER` — so it starts
even without you logged in.)

**launchd (macOS)** works the same way via a `LaunchAgent` plist with
`RunAtLoad`/`KeepAlive` — happy to write one out if that's your
platform, just ask.

Whatever you use, the laptop still needs to be powered on and *awake*
(not asleep) for the websocket connection to receive ticks — sleep
just pauses everything until it wakes back up, at which point `feed.py`
reconnects on its own.

## What gets written to disk

- `state/ladder_state.json` — current position, cost basis, P&L,
  signal counts, and which ISO week each symbol's baseline was last
  computed for. Rewritten atomically after every signal.
- `logs/signals.jsonl` — append-only audit log, one line per BUY/SELL
  signal (timestamp, symbol, side, price, outcome).
- `logs/ladder.log` — general run log (also echoed to stdout).

None of these are committed to git (`.gitignore` excludes them) —
they're machine-local runtime state, not source.

## CLI reference

```
python3 runner.py --help
```

Key flags: `--symbols`, `--step`, `--levels`, `--qty`, `--method`
(baseline calc: `friday_close` / `week_avg_close` / `week_vwap` /
`week_midpoint`), `--baseline` (manual first-week override),
`--feed` (`iex` or `sip`, matching your Alpaca market-data plan),
`--report-every-s` (status-report cadence).

## Tests

```bash
python3 tests/test_ladder_strategy.py
```
50 checks, ported from the original project's test suite — the
strategy math itself (`ladder_strategy.py`) is unchanged from
`fpga-tick-engine`, only its tick source changed.

## Where this came from

Extracted from `fpga-tick-engine`'s `host/ladder_strategy.py`, which
originally received ticks via a round trip through a physical FPGA
board (Alpaca trade -> UART -> board -> UART echo -> strategy). That
coupling is what made it FPGA-dependent even though the strategy logic
itself never touched the hardware. Here, `feed.py` replaces that whole
path with a direct Alpaca websocket connection, and `runner.py` adds
what a long-running standalone process needs that the original CLI
(a single trading session, run fresh each morning) didn't: weekly
baseline re-anchoring while running, and state persistence across
restarts.

`ladder_strategy.py`, `scorecard.py` (formerly `compare.py`), `costs.py`,
and `tick_protocol.py` are trimmed ports — logic unchanged, only the
parts specific to comparing against other strategies (SMA/EMA/VWAP)
or gating through a shared risk policy were dropped, since this
project only ever runs the one strategy.

## Pushing to git

```bash
git init
git add -A
git commit -m "Initial commit: standalone ladder strategy runner"
git remote add origin <your-new-repo-url>
git push -u origin main
```
# ladder-trader
