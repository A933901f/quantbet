#!/usr/bin/env bash
# LIVE: Telegram enabled, v4.2 frozen rules, keep-alive wrapper (systemd-inhibit / caffeinate) + heartbeat.
#   scripts/live.sh                 # = --live --rules v4.2 --cups
#   scripts/live.sh --unfreeze      # extra args are passed through
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs data
export QB_ALERT_RULES=v4.2
QB_RUNNER=scripts/live.py exec scripts/run_live.sh --live --rules v4.2 --cups "$@"
