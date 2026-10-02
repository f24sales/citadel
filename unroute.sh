#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec /usr/bin/python3 -s "$SCRIPT_DIR/functions/unroute_tailscale.py" --root "$SCRIPT_DIR" "$@"
