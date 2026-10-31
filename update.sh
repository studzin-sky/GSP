#!/usr/bin/env bash
# Update Garmin strength workout templates with your latest reps/weight.
#   ./update.sh              preview changes from the last 7 days, then confirm
#   ./update.sh -n           dry run
#   ./update.sh ui           open local web UI
#   ./update.sh login        first-time login
#   ./update.sh --help       all options
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if [[ ! -x .venv/bin/python ]]; then
  echo "Setting up virtualenv..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

exec .venv/bin/python progression.py "$@"
