#!/bin/sh
# Run after Tailscale is ready, before applications bind their ports.
set -eu

[ "${TAILSCALE_SERVE_RESET:-0}" = "1" ] || exit 0
exec tailscale serve reset
