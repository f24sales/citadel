#!/usr/bin/env bash
set -euo pipefail

[ "$#" -eq 0 ] || { echo 'Usage: ./setup.sh (no arguments)' >&2; exit 2; }
SCRIPT_DIR="$(dirname "$(readlink -f -- "${BASH_SOURCE[0]}")")"
cd "$SCRIPT_DIR"
USER_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"

# config.sh is hardlinked from SCRIPTS/safrano9999/config/config.sh.
# Configure and render everything; do not build, pull, link or start services.
"$SCRIPT_DIR/config.sh"
"$SCRIPT_DIR/set_daemon.sh" --render-only
python3 -s "$SCRIPT_DIR/functions/runtime_state.py"

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

# Read settings through the application's shared loader and transport resolver.
caddy_selected="$(PYTHONPATH="$SCRIPT_DIR:$SCRIPT_DIR/functions" python3 -s -c '
from python_header import get_int
from webui_transport import unix_socket_path
print(int(get_int("CADDYFILE_START") > 0 or unix_socket_path() is not None))
')"
if [[ "$caddy_selected" == 1 ]]; then
    printf '\nRecommended bind mount in your Caddy Quadlet:\n'
    printf '  Volume=%s/CITADEL_DATA/CADDY:/CADDY:ro,z\n' "$SCRIPT_DIR"
    printf '  Unix WebUI, if selected: reverse_proxy unix//CADDY/citadel.sock\n'
    printf '  Generated routes, if selected: import /CADDY/Caddyfile\n'
    printf 'Keep this mount unchanged when switching between service and container.\n'
fi
