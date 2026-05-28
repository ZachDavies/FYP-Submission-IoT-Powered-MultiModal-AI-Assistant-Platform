#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory so paths work regardless of where you call it from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

nix-shell --run "
  cd \"$SCRIPT_DIR\"

  cd dashboard && python app.py &
  cd \"$SCRIPT_DIR\"

  python orchestrator/main.py
" 2> >(grep -v 'ALSA lib' >&2)