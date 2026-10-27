#!/usr/bin/env bash
# Keep-alive wrapper for the live poller / paper dry run.
#
#   scripts/run_live.sh [runner args...]          # default runner: scripts/paper_dry_run.py
#   QB_RUNNER="-m quantbet.main" scripts/run_live.sh
#
# Linux : systemd-inhibit --what=idle:sleep (needs a reachable logind/D-Bus; falls back gracefully)
# macOS : caffeinate -i
# Windows: not handled here - the Python runner calls SetThreadExecutionState (quantbet.heartbeat.prevent_sleep_windows)
# NOTE: none of this can stop a VM/container/host-level pause (e.g. a paused cloud box). The heartbeat
#       monitor detects and logs such gaps and forces a scoreboard refresh on resume.
set -u
cd "$(dirname "$0")/.."
PY="${QB_PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
RUNNER="${QB_RUNNER:-scripts/paper_dry_run.py}"
WHY="quantbet live poller"

run_plain() { export QB_KEEPALIVE="${QB_KEEPALIVE:-none}"; exec "$PY" $RUNNER "$@"; }

case "$(uname -s)" in
  Linux)
    if command -v systemd-inhibit >/dev/null 2>&1 && \
       systemd-inhibit --what=idle:sleep --why="$WHY (probe)" true >/dev/null 2>&1; then
      echo "[run_live] keep-alive: systemd-inhibit --what=idle:sleep" >&2
      export QB_KEEPALIVE="systemd-inhibit"
      exec systemd-inhibit --what=idle:sleep --why="$WHY" --mode=block "$PY" $RUNNER "$@"
    fi
    echo "[run_live] keep-alive: systemd-inhibit unavailable/not permitted here - running without inhibitor" >&2
    export QB_KEEPALIVE="none (systemd-inhibit unavailable)"
    run_plain "$@" ;;
  Darwin)
    if command -v caffeinate >/dev/null 2>&1; then
      echo "[run_live] keep-alive: caffeinate -i" >&2
      export QB_KEEPALIVE="caffeinate"
      exec caffeinate -i "$PY" $RUNNER "$@"
    fi
    run_plain "$@" ;;
  *)
    echo "[run_live] no shell-level keep-alive for $(uname -s); Windows uses SetThreadExecutionState in the runner" >&2
    run_plain "$@" ;;
esac
