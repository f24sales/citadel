#!/usr/bin/env bash
set -euo pipefail

[ "$#" -eq 0 ] || { echo 'Usage: ./setup.sh (no arguments)' >&2; exit 2; }
SCRIPT_DIR="$(dirname "$(readlink -f -- "${BASH_SOURCE[0]}")")"
USER_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"

# config.sh is hardlinked from SCRIPTS/safrano9999/config/config.sh.
# Configure and render everything; do not build, pull, link or start services.
"$SCRIPT_DIR/config.sh"
"$SCRIPT_DIR/set_daemon.sh" --render-only
python3 -s "$SCRIPT_DIR/functions/runtime_state.py"
mkdir -p "$SCRIPT_DIR/CADDY" "$SCRIPT_DIR/icons"

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

# Reuse the renderer's conditional mount decision, not another env parser.
quadlet="$SCRIPT_DIR/$quadlet_name.container"
if [[ -f "$quadlet" ]]; then
    caddy_mount="$(sed -n 's|^Volume=\(.*\):/opt/safrano9999/CITADEL/CADDY:[^:]*$|\1|p' "$quadlet")"
    if [[ -n "$caddy_mount" ]]; then
        printf '\nRecommended bind mount in your Caddy Quadlet:\n'
        printf '  Volume=%s:/CADDY:ro,z\n' "$caddy_mount"
        printf '  Unix WebUI, if selected: reverse_proxy unix//CADDY/citadel.sock\n'
        printf '  Generated routes, if selected: import /CADDY/Caddyfile\n'
        printf 'Keep this mount unchanged when switching between service and container.\n'
    fi
fi
