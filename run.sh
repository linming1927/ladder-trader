#!/usr/bin/env bash
# run.sh — keep the ladder runner alive as long as this script is
# running. feed.py already reconnects on its own for network drops;
# this wrapper covers the other case — the python PROCESS itself
# dying (uncaught exception, `kill -9`, etc.) — by just restarting it.
#
# Usage:
#   ./run.sh                       # SPY,QQQ, all other defaults
#   ./run.sh --symbols SPY,QQQ,AAPL --levels 4
#
# Leave this running in a terminal, tmux/screen session, or as a
# systemd/launchd service — see README.md for options. Ctrl-C stops
# it for good (that's SIGINT, not a crash, so the loop below exits
# instead of restarting).

set -u
cd "$(dirname "$0")"

trap 'echo "[run.sh] stopping."; exit 0' INT TERM

while true; do
  python3 runner.py "$@"
  code=$?
  echo "[run.sh] runner.py exited with code $code — restarting in 10s" \
       "(Ctrl-C to stop for good)"
  sleep 10
done
