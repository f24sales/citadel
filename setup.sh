#!/usr/bin/env bash
set -euo pipefail

[ "$#" -eq 0 ] || { echo 'Usage: ./setup.sh (no arguments)' >&2; exit 2; }
SCRIPT_DIR="$(dirname "$(readlink -f -- "${BASH_SOURCE[0]}")")"
USER_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"

# config.sh is hardlinked from SCRIPTS/safrano9999/config/config.sh.
# Configure and render everything; do not build, pull, link or start services.
"$SCRIPT_DIR/config.sh"
"$SCRIPT_DIR/set_daemon.sh" --render-only
mkdir -p "$SCRIPT_DIR/CADDY"

printf '\nHost service (choose this OR the container):\n'
printf '  mkdir -p %q && ln -s %q %q\n' \
    "$USER_CONFIG_DIR/systemd/user" "$SCRIPT_DIR/citadel.service" \
    "$USER_CONFIG_DIR/systemd/user/citadel.service"
printf '\nContainer Quadlet:\n'
quadlet_name="${SCRIPT_DIR##*/}"
quadlet_name="${quadlet_name,,}"
printf '  mkdir -p %q && ln -s %q %q\n' \
    "$USER_CONFIG_DIR/containers/systemd" "$SCRIPT_DIR/$quadlet_name.container" \
    "$USER_CONFIG_DIR/containers/systemd/citadel.container"
printf '\nAfter choosing one: systemctl --user daemon-reload\n'
